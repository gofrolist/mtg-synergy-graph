"""Justified-divergence tests (Unit 3 of plan 2026-06-10-001, R9).

Pure tests exercise :func:`justified_divergence_for_commander` (the
thin wrapper applying the hidden-gems plausibility-gate constants to
the wider ALL-SECTIONS reference set) with hand-built cohorts;
DB-shell tests build a synthetic synergy.db from the committed Forge
fixture cards under ``tmp_path`` ONLY (the session conftest sentinel
fails the run on any stray ``*.db`` under the repo root or
``data/``), so the justified view is exercised against a real
``engine.page()`` top-30 (Korvold ranks six fixture cards).

Seeding helpers are local copies of the ones in
``tests/bench/test_forensics_classify.py`` /
``test_forensics_metrics.py`` (tests/ is not an importable package).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.forensics import (
    N_RULES_BIN_LABELS,
    RATIO_BIN_LABELS,
    JustifiedDivergence,
    bucket_proportions,
    classify_commander_misses,
    compute_forensics,
    justified_divergence_for_commander,
    load_nonpositive_listed,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

KORVOLD = "Korvold, Fae-Cursed King"
KORVOLD_SLUG = "korvold-fae-cursed-king"

#: Korvold's full fixture-DB live ranking (see test_engine_api.py /
#: test_forensics_metrics.py) — six cards, all inside the top-30.
KORVOLD_TOP = (
    "Tireless Tracker",
    "Phyrexian Altar",
    "Dockside Extortionist",
    "Bloodghast",
    "Scute Swarm",
    "Sol Ring",
)


# ---------------------------------------------------------------------------
# Seeding helpers — local copies from sibling forensics test modules
# ---------------------------------------------------------------------------


def _write_fixture(path: Path, commanders: list[object]) -> None:
    """Minimal PinnedFixture JSON (schema v2 shape)."""
    payload = {
        "schema_version": 2,
        "config_hash": "irrelevant-for-forensics",
        "created_at": "2026-06-10T00:00:00+00:00",
        "entries": [{"commander": c, "scores": {}} for c in commanders],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_tensor_row(
    conn: sqlite3.Connection,
    commander: str,
    candidate: str,
    config_hash: str,
    *,
    rule_id: str = "test_rule",
    contribution: float = 1.0,
) -> None:
    conn.execute(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, config_hash, computed_at) "
        "VALUES (?, ?, ?, ?, 1.0, 1, ?, '2026-06-10T00:00:00+00:00')",
        (commander, candidate, rule_id, contribution, config_hash),
    )


def _make_tags_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)"
        )
        conn.executemany("INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pure core — gate split, nonpositive handling, saturation, bins
# ---------------------------------------------------------------------------


def _rows(*entries: tuple[str, int, float]) -> list[tuple[str, str, float]]:
    """Expand (candidate, n_rules, per_rule_contribution) shorthand."""
    out: list[tuple[str, str, float]] = []
    for cand, n_rules, per_rule in entries:
        out.extend((cand, f"rule_{i}", per_rule) for i in range(n_rules))
    return out


class TestJustifiedDivergencePure:
    def test_gate_pass_and_fail_split(self) -> None:
        """Divergent pick passing the gate → justified; failing → unjustified."""
        # Cohort: Gem total 2.0 over 2 rules; Dud total 0.5 over 1
        # rule. Median of positive totals = 1.25, so Dud fails both
        # gate legs while Gem passes via N_rules >= 2.
        jd = justified_divergence_for_commander(
            ["Gem", "Dud", "Listed"],
            frozenset({"Listed"}),
            frozenset(),
            _rows(("Gem", 2, 1.0), ("Dud", 1, 0.5)),
        )
        assert jd.divergent == 2
        assert jd.justified_cards == ("Gem",)
        assert jd.unjustified_cards == ("Dud",)
        assert jd.justified_divergences == 1
        assert jd.unjustified_divergences == 1
        assert jd.gate_pass_rate == pytest.approx(0.5)
        assert jd.gate_saturated is False
        assert jd.listed_nonpositive == 0

    def test_contribution_leg_justifies_single_rule_pick(self) -> None:
        """The median leg (same constants as hidden_gems) can justify a
        pick with fewer than 2 firing rules."""
        # Totals: Big 5.0, A 1.0, B 1.0 → median 1.0; Big 5.0 > 1.0.
        jd = justified_divergence_for_commander(
            ["Big"],
            frozenset(),
            frozenset(),
            _rows(("Big", 1, 5.0), ("A", 1, 1.0), ("B", 1, 1.0)),
        )
        assert jd.justified_cards == ("Big",)
        assert jd.n_rules_distribution["0-1"] == 1
        assert jd.ratio_distribution[">5"] == 0  # ratio is exactly 5.0
        assert jd.ratio_distribution["2-5"] == 1

    def test_listed_nonpositive_separate_count_not_divergent(self) -> None:
        """A pick listed at synergy <= 0 lands in listed_nonpositive and
        never in the divergent denominator."""
        jd = justified_divergence_for_commander(
            ["Negative Pick", "True Gem"],
            frozenset(),
            frozenset({"Negative Pick"}),
            _rows(("True Gem", 2, 1.0)),
        )
        assert jd.listed_nonpositive == 1
        assert jd.divergent == 1
        assert jd.justified_cards == ("True Gem",)
        assert "Negative Pick" not in jd.justified_cards + jd.unjustified_cards
        assert jd.gate_pass_rate == pytest.approx(1.0)

    def test_zero_divergent_pass_rate_is_none(self) -> None:
        jd = justified_divergence_for_commander(
            ["Listed A", "Listed B"],
            frozenset({"Listed A", "Listed B"}),
            frozenset(),
            [],
        )
        assert jd.divergent == 0
        assert jd.gate_pass_rate is None
        assert jd.gate_saturated is False
        assert all(v == 0 for v in jd.n_rules_distribution.values())
        assert all(v == 0 for v in jd.ratio_distribution.values())

    def test_all_pass_gate_saturated(self) -> None:
        """All divergent picks pass → pass-rate 1.0 and the too-loose
        signal (gate_saturated) is derivable for the Unit-4 renderer."""
        jd = justified_divergence_for_commander(
            ["Gem A", "Gem B"],
            frozenset(),
            frozenset(),
            _rows(("Gem A", 2, 1.0), ("Gem B", 3, 1.0)),
        )
        assert jd.gate_pass_rate == pytest.approx(1.0)
        assert jd.gate_saturated is True
        assert jd.unjustified_cards == ()

    def test_stratification_bins_hand_built(self) -> None:
        """Hand-built cohort hitting every bin of both distributions.

        Totals: A 2, B 3, C 4, F 6, D 12, E 30 → median 5.0.
        Ratios: A 0.4 / B 0.6 / C 0.8 (<=1), F 1.2 (1-2), D 2.4
        (2-5), E 6.0 (>5). N_rules: E 1 (0-1), A+F 2, B 3, C 4, D 6
        (5+). All six pass the gate (A–D, F via N_rules; E via the
        median leg).
        """
        rows = _rows(
            ("A", 2, 1.0),
            ("B", 3, 1.0),
            ("C", 4, 1.0),
            ("D", 6, 2.0),
            ("E", 1, 30.0),
            ("F", 2, 3.0),
        )
        jd = justified_divergence_for_commander(
            ["A", "B", "C", "D", "E", "F"],
            frozenset(),
            frozenset(),
            rows,
        )
        assert jd.justified_divergences == 6
        assert dict(jd.n_rules_distribution) == {"0-1": 1, "2": 2, "3": 1, "4": 1, "5+": 1}
        assert dict(jd.ratio_distribution) == {"<=1": 3, "1-2": 1, "2-5": 1, ">5": 1}
        # Distributions stratify the JUSTIFIED picks only.
        assert sum(jd.n_rules_distribution.values()) == jd.justified_divergences
        assert sum(jd.ratio_distribution.values()) == jd.justified_divergences

    def test_distributions_carry_every_bin_key(self) -> None:
        jd = justified_divergence_for_commander(["X"], frozenset(), frozenset(), [])
        assert tuple(jd.n_rules_distribution) == N_RULES_BIN_LABELS
        assert tuple(jd.ratio_distribution) == RATIO_BIN_LABELS

    def test_duplicate_top_30_raises(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            justified_divergence_for_commander(["X", "X"], frozenset(), frozenset(), [])

    def test_distribution_mappings_are_frozen(self) -> None:
        jd = justified_divergence_for_commander(["X"], frozenset(), frozenset(), [])
        with pytest.raises(TypeError):
            jd.n_rules_distribution["2"] = 99  # type: ignore[index]

    def test_determinism_repeat_call_equality(self) -> None:
        args = (
            ["Gem", "Dud"],
            frozenset({"Other"}),
            frozenset(),
            _rows(("Gem", 2, 1.0), ("Dud", 1, 0.5)),
        )
        assert justified_divergence_for_commander(*args) == justified_divergence_for_commander(*args)

    def test_pure_core_entry_has_no_justified_view(self) -> None:
        """Pure-core classify path leaves the Unit-3 field at its
        default (None) — only compute_forensics populates it."""
        entry = classify_commander_misses("Someone", ())
        assert entry.justified_divergence is None


# ---------------------------------------------------------------------------
# DB-shell integration — Forge fixture engine, tmp_path DBs only
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synergy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """synergy.db from the committed Forge fixture cards + tensor rows.

    Korvold's live top-30 is :data:`KORVOLD_TOP`. Tensor cohort at the
    CURRENT config hash: Tireless Tracker fires 2 rules (total 2.0),
    Dockside Extortionist fires 1 rule (total 0.5) → cohort median
    1.25, so Tracker passes the gate (N_rules leg) and Dockside fails
    both legs. Scute Swarm / Sol Ring hold no tensor rows.
    """
    db_path = tmp_path_factory.mktemp("forensics_justified") / "synergy.db"
    config_hash = compute_config_hash()
    conn = open_db(db_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        _seed_tensor_row(conn, KORVOLD, "Tireless Tracker", config_hash, rule_id="rule_a", contribution=1.0)
        _seed_tensor_row(conn, KORVOLD, "Tireless Tracker", config_hash, rule_id="rule_b", contribution=1.0)
        _seed_tensor_row(conn, KORVOLD, "Dockside Extortionist", config_hash, rule_id="rule_a", contribution=0.5)
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture()
def paths(synergy_db: Path, tmp_path: Path) -> dict[str, Path]:
    """tags.db + fixture: one positive listing inside our top-30
    (Phyrexian Altar), one nonpositive listing inside our top-30
    (Bloodghast at -0.5), one positive unranked label (a miss)."""
    tags_path = tmp_path / "tags.db"
    fixture_path = tmp_path / "fixture.json"
    _make_tags_db(
        tags_path,
        [
            (KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", 2.0),
            (KORVOLD_SLUG, "Bloodghast", "Top Cards", -0.5),
            (KORVOLD_SLUG, "Imaginary Synergy Piece", "Creatures", 0.5),
        ],
    )
    _write_fixture(fixture_path, [KORVOLD])
    return {"db": synergy_db, "tags": tags_path, "fixture": fixture_path}


def _run(paths: dict[str, Path]):
    return compute_forensics(
        db_path=paths["db"],
        tags_path=paths["tags"],
        fixture_path=paths["fixture"],
        independent_check=False,
    )


class TestJustifiedDivergenceIntegration:
    def test_end_to_end_justified_view(self, paths: dict[str, Path]) -> None:
        report = _run(paths)
        entry = report.entries[0]
        assert entry.live_top_30 == KORVOLD_TOP

        jd = entry.justified_divergence
        assert isinstance(jd, JustifiedDivergence)
        # Phyrexian Altar is listed positive (not divergent); Bloodghast
        # is listed nonpositive; the remaining four picks are divergent.
        assert jd.divergent == 4
        assert jd.listed_nonpositive == 1
        assert jd.justified_cards == ("Tireless Tracker",)
        assert jd.unjustified_cards == ("Dockside Extortionist", "Scute Swarm", "Sol Ring")
        assert jd.gate_pass_rate == pytest.approx(0.25)
        assert jd.gate_saturated is False
        # Tracker: 2 rules → "2"; ratio 2.0 / 1.25 = 1.6 → "1-2".
        assert dict(jd.n_rules_distribution) == {"0-1": 0, "2": 1, "3": 0, "4": 0, "5+": 0}
        assert dict(jd.ratio_distribution) == {"<=1": 0, "1-2": 1, "2-5": 0, ">5": 0}

    def test_nonpositive_listing_not_a_miss_and_buckets_unchanged(self, paths: dict[str, Path]) -> None:
        """Bloodghast (synergy -0.5) is excluded from the floored label
        set → never a miss, never divergent; bucket sums cover the
        misses only (annotation, not subtraction)."""
        report = _run(paths)
        entry = report.entries[0]
        miss_names = {m.card_name for m in entry.misses}
        assert miss_names == {"Imaginary Synergy Piece"}
        assert "Bloodghast" not in miss_names

        # Bucket sums equal the miss count even though the justified
        # view annotates 4 divergent / 1 justified / 1 nonpositive.
        assert sum(entry.bucket_counts.values()) == len(entry.misses) == 1
        proportions = bucket_proportions(entry.bucket_counts)
        assert proportions is not None
        assert sum(proportions.values()) == pytest.approx(100.0)
        jd = entry.justified_divergence
        assert jd is not None
        assert jd.justified_divergences == 1
        assert jd.listed_nonpositive == 1

    def test_load_nonpositive_listed_partitions_floored_set(self, paths: dict[str, Path]) -> None:
        """The unfloored membership query returns exactly the graded
        names the floored loader excludes."""
        conn = sqlite3.connect(paths["tags"])
        conn.row_factory = sqlite3.Row
        try:
            nonpositive = load_nonpositive_listed(conn, KORVOLD)
        finally:
            conn.close()
        assert nonpositive == frozenset({"Bloodghast"})

    def test_determinism_repeat_runs_equal(self, paths: dict[str, Path]) -> None:
        assert _run(paths) == _run(paths)

    def test_no_additional_db_connections_opened(self, paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """Unit-3 verification: the justified view derives from the live
        top-30 + tensor cohort + labels already in hand; its only new
        DB read (unfloored membership) rides the EXISTING tags.db
        connection. The whole run opens exactly the Unit-1/2
        connections: synergy.db twice (open_db + the shared engine)
        and tags.db once."""
        real_connect = sqlite3.connect
        opened: list[str] = []

        def _tracking_connect(database, *args, **kwargs):
            opened.append(str(database))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
        _run(paths)
        assert set(opened) == {str(paths["db"]), str(paths["tags"])}
        assert opened.count(str(paths["tags"])) == 1
        assert opened.count(str(paths["db"])) == 2
