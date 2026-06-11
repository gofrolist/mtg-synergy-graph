"""Forensics classification tests (Unit 1 of plan 2026-06-10-001).

Pure-core tests feed plain data into the five-bucket classifier;
DB-shell tests build synthetic synergy.db / tags.db pairs under
``tmp_path`` ONLY (the session conftest sentinel fails the run on any
stray ``*.db`` under the repo root or ``data/``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.forensics import (
    BUCKET_DATA_GAP,
    BUCKET_FILTERED,
    BUCKET_NEAR_MISS,
    BUCKET_NO_RULES,
    BUCKET_OUTRANKED,
    BUCKETS,
    REASON_CARD_ABSENT,
    REASON_COLOR_ILLEGAL,
    REASON_EMPTY_TYPES,
    REASON_FILTER_UNKNOWN,
    REASON_IS_COMMANDER,
    REASON_NO_PORTS,
    REASON_NON_EDH_TYPE,
    REASON_NOT_LEGAL,
    REASON_STAPLE_ONLY,
    REASON_UNKNOWN_PORTS,
    SKIP_REASON_ZERO_LABELS,
    CommanderForensics,
    ForensicsPreconditionError,
    LabelCard,
    RankedCandidate,
    aggregate_forensics,
    bucket_proportions,
    classify_commander_misses,
    classify_miss,
    compute_forensics,
    miss_universe_from_labels,
    normalize_label_name,
)
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.engine import UNRANKED_EDHREC_SENTINEL

COMMANDER = "General Gee"


# ---------------------------------------------------------------------------
# Pure-core helpers
# ---------------------------------------------------------------------------


def _label(name: str, synergy: float = 0.5, hs: bool = False) -> LabelCard:
    return LabelCard(name=name, synergy=synergy, hs_section_member=hs)


def _ranked(name: str, rank: int, score: float = 1.0) -> RankedCandidate:
    return RankedCandidate(name=name, rank=rank, total_score=score, cmc=2.0, edhrec_rank=100)


def _card_row(
    name: str,
    color_identity: str = "G",
    card_types: str = "Creature",
    legal: int = 1,
) -> dict[str, object]:
    return {
        "name": name,
        "color_identity": color_identity,
        "card_types": card_types,
        "legal_commander": legal,
    }


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


class TestNormalizeLabelName:
    def test_exact_match(self) -> None:
        assert normalize_label_name("Sol Ring", frozenset({"Sol Ring"})) == "Sol Ring"

    def test_front_face_split(self) -> None:
        known = frozenset({"Malakir Rebirth"})
        assert normalize_label_name("Malakir Rebirth // Malakir Mire", known) == "Malakir Rebirth"

    def test_unmatched_returns_none(self) -> None:
        assert normalize_label_name("Unknown Card", frozenset({"Sol Ring"})) is None


# ---------------------------------------------------------------------------
# Miss universe — tie closure
# ---------------------------------------------------------------------------


class TestMissUniverse:
    def test_tie_at_boundary_includes_all_tie_members(self) -> None:
        """29 unique-synergy labels + 3 tied at the 30th value → all 32 in."""
        labels = {f"Card {i:02d}": 100.0 - i for i in range(29)}
        labels.update({"Tie A": 50.0, "Tie B": 50.0, "Tie C": 50.0})
        universe = miss_universe_from_labels(labels, set())
        assert len(universe) == 32
        assert {lc.name for lc in universe} >= {"Tie A", "Tie B", "Tie C"}

    def test_no_tie_truncates_at_top_n(self) -> None:
        labels = {f"Card {i:02d}": 100.0 - i for i in range(40)}
        universe = miss_universe_from_labels(labels, set())
        assert len(universe) == 30
        assert universe[0].name == "Card 00"

    def test_fewer_than_top_n_keeps_all(self) -> None:
        labels = {"A": 3.0, "B": 2.0}
        universe = miss_universe_from_labels(labels, set())
        assert [lc.name for lc in universe] == ["A", "B"]

    def test_hs_membership_flagged(self) -> None:
        universe = miss_universe_from_labels({"A": 3.0, "B": 2.0}, {"A"})
        flags = {lc.name: lc.hs_section_member for lc in universe}
        assert flags == {"A": True, "B": False}

    def test_rejects_nonpositive_top_n(self) -> None:
        with pytest.raises(ValueError, match="top_n"):
            miss_universe_from_labels({"A": 1.0}, set(), top_n=0)


# ---------------------------------------------------------------------------
# classify_miss — bucket precedence + sub-tags
# ---------------------------------------------------------------------------


def _classify(label: LabelCard, **overrides) -> object:
    """classify_miss with sensible defaults; overrides per scenario."""
    kwargs: dict = {
        "commander": COMMANDER,
        "resolved_name": label.name,
        "rank": None,
        "has_tensor_rows": False,
        "card_row": _card_row(label.name),
        "commander_identity": frozenset({"G"}),
        "commander_names": frozenset({COMMANDER}),
        "port_stats": (3, 0),
    }
    kwargs.update(overrides)
    return classify_miss(label, **kwargs)


class TestClassifyMiss:
    def test_rank_inside_top_30_raises(self) -> None:
        with pytest.raises(ValueError, match="not a miss"):
            _classify(_label("X"), rank=30)

    @pytest.mark.parametrize("rank", [31, 45, 60])
    def test_near_miss_window(self, rank: int) -> None:
        record = _classify(_label("X"), rank=rank)
        assert record.bucket == BUCKET_NEAR_MISS
        assert record.reason is None

    def test_outranked_at_61(self) -> None:
        record = _classify(_label("X"), rank=61, has_tensor_rows=True)
        assert record.bucket == BUCKET_OUTRANKED
        assert record.reason is None

    def test_outranked_staple_only_when_no_tensor_rows(self) -> None:
        record = _classify(_label("X"), rank=200, has_tensor_rows=False)
        assert record.bucket == BUCKET_OUTRANKED
        assert record.reason == REASON_STAPLE_ONLY

    def test_filtered_color_illegal(self) -> None:
        record = _classify(
            _label("Off Color"),
            has_tensor_rows=True,
            card_row=_card_row("Off Color", color_identity="W"),
        )
        assert record.bucket == BUCKET_FILTERED
        assert record.reason == REASON_COLOR_ILLEGAL

    def test_filtered_not_legal(self) -> None:
        record = _classify(_label("Acorn"), has_tensor_rows=True, card_row=_card_row("Acorn", legal=0))
        assert record.bucket == BUCKET_FILTERED
        assert record.reason == REASON_NOT_LEGAL

    def test_filtered_non_edh_type(self) -> None:
        record = _classify(
            _label("Jund"),
            has_tensor_rows=True,
            card_row=_card_row("Jund", color_identity="", card_types="Plane"),
        )
        assert record.bucket == BUCKET_FILTERED
        assert record.reason == REASON_NON_EDH_TYPE

    def test_filtered_empty_types(self) -> None:
        record = _classify(
            _label("Malformed"),
            has_tensor_rows=True,
            card_row=_card_row("Malformed", card_types=""),
        )
        assert record.bucket == BUCKET_FILTERED
        assert record.reason == REASON_EMPTY_TYPES

    def test_filtered_is_commander(self) -> None:
        record = _classify(
            _label(COMMANDER),
            resolved_name=COMMANDER,
            has_tensor_rows=True,
            card_row=_card_row(COMMANDER),
        )
        assert record.bucket == BUCKET_FILTERED
        assert record.reason == REASON_IS_COMMANDER

    def test_filtered_reason_unknown_when_legal_and_unranked(self) -> None:
        """Legal + tensor rows + unranked → tensor-staleness diagnostic."""
        record = _classify(_label("Ghost Tensor"), has_tensor_rows=True)
        assert record.bucket == BUCKET_FILTERED
        assert record.reason == REASON_FILTER_UNKNOWN

    def test_data_gap_card_absent_with_name_unmatched(self) -> None:
        record = _classify(_label("EDHREC Only Name"), resolved_name=None, card_row=None)
        assert record.bucket == BUCKET_DATA_GAP
        assert record.reason == REASON_CARD_ABSENT
        assert record.name_unmatched is True

    def test_data_gap_no_ports(self) -> None:
        record = _classify(_label("Portless"), port_stats=(0, 0))
        assert record.bucket == BUCKET_DATA_GAP
        assert record.reason == REASON_NO_PORTS

    def test_data_gap_unknown_ports_above_half(self) -> None:
        record = _classify(_label("Mystery"), port_stats=(4, 3))
        assert record.bucket == BUCKET_DATA_GAP
        assert record.reason == REASON_UNKNOWN_PORTS

    def test_exactly_half_unknown_is_no_rules(self) -> None:
        """Threshold is strictly > 50% — half-UNKNOWN port data is usable."""
        record = _classify(_label("Half"), port_stats=(4, 2))
        assert record.bucket == BUCKET_NO_RULES
        assert record.reason is None

    def test_no_rules_with_clean_ports(self) -> None:
        record = _classify(_label("Clean"), port_stats=(5, 0))
        assert record.bucket == BUCKET_NO_RULES
        assert record.reason is None


# ---------------------------------------------------------------------------
# classify_commander_misses — orchestration
# ---------------------------------------------------------------------------


def _one_per_bucket_entry() -> CommanderForensics:
    """Synthetic commander with exactly one miss in each bucket."""
    universe = (
        _label("Near Miss Card", 0.9, hs=True),
        _label("Outranked Card", 0.8),
        _label("Filtered Card", 0.7),
        _label("Gap Card", 0.6),
        _label("No Rules Card", 0.5),
        _label("Hit Card", 0.4),  # in our top-30 → not a miss
    )
    ranking = (
        _ranked("Hit Card", 1),
        _ranked("Near Miss Card", 40),
        _ranked("Outranked Card", 100),
    )
    known = frozenset({"Near Miss Card", "Outranked Card", "Filtered Card", "No Rules Card", "Hit Card", COMMANDER})
    return classify_commander_misses(
        COMMANDER,
        universe,
        ranking=ranking,
        known_names=known,
        tensor_candidates=frozenset({"Outranked Card", "Filtered Card"}),
        card_rows={
            "Filtered Card": _card_row("Filtered Card", color_identity="W"),
            "No Rules Card": _card_row("No Rules Card"),
        },
        commander_identity=frozenset({"G"}),
        port_stats={"No Rules Card": (3, 0)},
    )


class TestClassifyCommanderMisses:
    def test_one_miss_per_bucket(self) -> None:
        entry = _one_per_bucket_entry()
        assert dict(entry.bucket_counts) == dict.fromkeys(BUCKETS, 1)
        by_name = {m.card_name: m for m in entry.misses}
        assert by_name["Near Miss Card"].bucket == BUCKET_NEAR_MISS
        assert by_name["Outranked Card"].bucket == BUCKET_OUTRANKED
        assert by_name["Filtered Card"].bucket == BUCKET_FILTERED
        assert by_name["Filtered Card"].reason == REASON_COLOR_ILLEGAL
        assert by_name["Gap Card"].bucket == BUCKET_DATA_GAP
        assert by_name["Gap Card"].reason == REASON_CARD_ABSENT
        assert by_name["Gap Card"].name_unmatched is True
        assert by_name["No Rules Card"].bucket == BUCKET_NO_RULES

    def test_proportions_sum_to_100(self) -> None:
        entry = _one_per_bucket_entry()
        proportions = bucket_proportions(entry.bucket_counts)
        assert proportions is not None
        assert sum(proportions.values()) == pytest.approx(100.0)

    def test_label_in_top_30_is_not_a_miss(self) -> None:
        entry = _one_per_bucket_entry()
        assert "Hit Card" not in {m.card_name for m in entry.misses}
        assert "Hit Card" in entry.live_top_30

    def test_hs_flag_carried_onto_miss_record(self) -> None:
        entry = _one_per_bucket_entry()
        by_name = {m.card_name: m for m in entry.misses}
        assert by_name["Near Miss Card"].hs_section_member is True
        assert by_name["Outranked Card"].hs_section_member is False

    def test_zero_label_commander_is_skipped(self) -> None:
        entry = classify_commander_misses(COMMANDER, ())
        assert entry.skipped is True
        assert entry.skip_reason == SKIP_REASON_ZERO_LABELS
        assert entry.misses == ()

    def test_zero_miss_commander_not_skipped(self) -> None:
        universe = (_label("Hit Card", 0.9),)
        entry = classify_commander_misses(
            COMMANDER,
            universe,
            ranking=(_ranked("Hit Card", 1),),
            known_names=frozenset({"Hit Card"}),
        )
        assert entry.skipped is False
        assert entry.misses == ()
        assert sum(entry.bucket_counts.values()) == 0
        assert bucket_proportions(entry.bucket_counts) is None

    def test_determinism_repeat_run_equality(self) -> None:
        assert _one_per_bucket_entry() == _one_per_bucket_entry()


# ---------------------------------------------------------------------------
# aggregate_forensics
# ---------------------------------------------------------------------------


class TestAggregateForensics:
    def test_skipped_commanders_excluded_from_aggregates(self) -> None:
        skipped = classify_commander_misses("Labelless", ())
        full = _one_per_bucket_entry()
        report = aggregate_forensics([skipped, full])
        assert report.skipped_commanders == ("Labelless",)
        assert [e.commander for e in report.entries] == [COMMANDER]
        assert dict(report.aggregate_bucket_counts) == dict.fromkeys(BUCKETS, 1)
        assert report.total_misses == 5

    def test_zero_miss_commander_stays_in_entries(self) -> None:
        entry = classify_commander_misses(
            COMMANDER,
            (_label("Hit", 0.9),),
            ranking=(_ranked("Hit", 1),),
            known_names=frozenset({"Hit"}),
        )
        report = aggregate_forensics([entry])
        assert report.skipped_commanders == ()
        assert report.total_misses == 0
        assert len(report.entries) == 1


# ---------------------------------------------------------------------------
# RankedCandidate sort-key capture
# ---------------------------------------------------------------------------


def test_production_sort_key_shape() -> None:
    rc = RankedCandidate(name="X", rank=3, total_score=4.5, cmc=2.0, edhrec_rank=77)
    assert rc.production_sort_key == (-4.5, 2.0, 77, "X")


# ---------------------------------------------------------------------------
# DB-shell tests — synthetic tmp_path DBs only
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


def _seed_card(
    conn: sqlite3.Connection,
    name: str,
    *,
    color_identity: str = "G",
    card_types: str = "Creature",
    cmc: float | None = 2.0,
    edhrec_rank: int | None = 100,
    legal: int = 1,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cards (name, color_identity, card_types, cmc, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, color_identity, card_types, cmc, edhrec_rank, legal),
    )


def _seed_tensor_row(conn: sqlite3.Connection, commander: str, candidate: str, config_hash: str) -> None:
    conn.execute(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, config_hash, computed_at) "
        "VALUES (?, ?, 'test_rule', 1.0, 1.0, 1, ?, '2026-06-10T00:00:00+00:00')",
        (commander, candidate, config_hash),
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


@pytest.fixture()
def forensics_paths(tmp_path: Path) -> dict[str, Path]:
    """Synthetic synergy.db + tags.db + fixture for one commander.

    Engine-visible cards produce no complement matches (no commander
    ports), so every label is unranked — exercising the FILTERED /
    DATA_GAP / NO_RULES side end-to-end. NEAR_MISS / OUTRANKED are
    covered at the pure level.
    """
    db_path = tmp_path / "synergy.db"
    tags_path = tmp_path / "tags.db"
    fixture_path = tmp_path / "fixture.json"
    config_hash = compute_config_hash()

    conn = open_db(db_path)
    try:
        slug_cmdr = COMMANDER  # "General Gee" → slug "general-gee"
        _seed_card(conn, slug_cmdr, card_types="Creature")
        # FILTERED color_illegal: off-color card with tensor rows.
        _seed_card(conn, "Off Color", color_identity="W")
        # FILTERED empty_types: malformed row with tensor rows.
        _seed_card(conn, "Malformed", card_types="")
        # FILTERED filter_reason_unknown: legal, tensor rows, unranked.
        _seed_card(conn, "Stale Tensor")
        # DATA_GAP no_ports: in cards, no port rows.
        _seed_card(conn, "Portless")
        # DATA_GAP unknown_ports: majority-UNKNOWN port shapes.
        _seed_card(conn, "Mystery Ports")
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Mystery Ports', 'trigger', 'TotallyNovelEvent')"
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Mystery Ports', 'effect', 'AnotherNovelThing')"
        )
        # NO_RULES: well-formed mapped port, no tensor rows, unranked.
        _seed_card(conn, "Plain Card")
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Plain Card', 'trigger', 'SpellCast')"
        )
        # Tensor rows at the CURRENT config hash.
        _seed_tensor_row(conn, COMMANDER, "Off Color", config_hash)
        _seed_tensor_row(conn, COMMANDER, "Malformed", config_hash)
        _seed_tensor_row(conn, COMMANDER, "Stale Tensor", config_hash)
        # is_commander canary: tensor row for the commander as candidate.
        _seed_tensor_row(conn, COMMANDER, COMMANDER, config_hash)
        conn.commit()
    finally:
        conn.close()

    hs = "High Synergy Cards"
    _make_tags_db(
        tags_path,
        [
            ("general-gee", "Off Color", hs, 0.9),
            ("general-gee", "Malformed", "Top Cards", 0.8),
            ("general-gee", "Stale Tensor", "Top Cards", 0.7),
            ("general-gee", COMMANDER, "Top Cards", 0.65),
            ("general-gee", "Portless", "Creatures", 0.6),
            ("general-gee", "Mystery Ports", "Creatures", 0.5),
            ("general-gee", "Plain Card", "Creatures", 0.4),
            ("general-gee", "EDHREC Only Name", "Creatures", 0.3),
            # Zero-label commander check: no rows for "Labelless Lass".
        ],
    )
    _write_fixture(fixture_path, [COMMANDER])
    return {"db": db_path, "tags": tags_path, "fixture": fixture_path}


class TestComputeForensicsIntegration:
    def test_end_to_end_buckets_and_reasons(self, forensics_paths: dict[str, Path]) -> None:
        report = compute_forensics(
            db_path=forensics_paths["db"],
            tags_path=forensics_paths["tags"],
            fixture_path=forensics_paths["fixture"],
        )
        assert report.skipped_commanders == ()
        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.commander == COMMANDER

        by_name = {m.card_name: m for m in entry.misses}
        assert by_name["Off Color"].bucket == BUCKET_FILTERED
        assert by_name["Off Color"].reason == REASON_COLOR_ILLEGAL
        assert by_name["Off Color"].hs_section_member is True
        assert by_name["Malformed"].reason == REASON_EMPTY_TYPES
        assert by_name["Stale Tensor"].reason == REASON_FILTER_UNKNOWN
        assert by_name[COMMANDER].bucket == BUCKET_FILTERED
        assert by_name[COMMANDER].reason == REASON_IS_COMMANDER
        assert by_name["Portless"].bucket == BUCKET_DATA_GAP
        assert by_name["Portless"].reason == REASON_NO_PORTS
        assert by_name["Mystery Ports"].reason == REASON_UNKNOWN_PORTS
        assert by_name["Plain Card"].bucket == BUCKET_NO_RULES
        assert by_name["EDHREC Only Name"].bucket == BUCKET_DATA_GAP
        assert by_name["EDHREC Only Name"].reason == REASON_CARD_ABSENT
        assert by_name["EDHREC Only Name"].name_unmatched is True

        assert dict(entry.bucket_counts) == {
            BUCKET_NEAR_MISS: 0,
            BUCKET_OUTRANKED: 0,
            BUCKET_FILTERED: 4,
            BUCKET_DATA_GAP: 3,
            BUCKET_NO_RULES: 1,
        }
        proportions = bucket_proportions(entry.bucket_counts)
        assert proportions is not None
        assert sum(proportions.values()) == pytest.approx(100.0)
        assert report.total_misses == 8

    def test_zero_label_commander_listed_in_skip_list(self, forensics_paths: dict[str, Path], tmp_path: Path) -> None:
        db_conn = open_db(forensics_paths["db"])
        try:
            _seed_card(db_conn, "Labelless Lass")
            db_conn.commit()
        finally:
            db_conn.close()
        _write_fixture(forensics_paths["fixture"], [COMMANDER, "Labelless Lass"])

        report = compute_forensics(
            db_path=forensics_paths["db"],
            tags_path=forensics_paths["tags"],
            fixture_path=forensics_paths["fixture"],
        )
        assert report.skipped_commanders == ("Labelless Lass",)
        assert [e.commander for e in report.entries] == [COMMANDER]

    def test_determinism_repeat_runs_equal(self, forensics_paths: dict[str, Path]) -> None:
        kwargs = {
            "db_path": forensics_paths["db"],
            "tags_path": forensics_paths["tags"],
            "fixture_path": forensics_paths["fixture"],
        }
        assert compute_forensics(**kwargs) == compute_forensics(**kwargs)

    def test_freshness_advisory_emitted(self, forensics_paths: dict[str, Path], capsys) -> None:
        compute_forensics(
            db_path=forensics_paths["db"],
            tags_path=forensics_paths["tags"],
            fixture_path=forensics_paths["fixture"],
        )
        err = capsys.readouterr().err
        assert "freshness cannot be cross-checked" in err

    def test_missing_commander_cards_row_warns_and_proceeds(
        self,
        forensics_paths: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        """A commander with no ``cards`` row gets a stderr warning naming
        it (advisory: identity falls back to empty, classification
        proceeds)."""
        from mtg_synergy_graph.bench import forensics as forensics_module

        real_load_card_rows = forensics_module.load_card_rows

        def _without_commander(conn, names):
            rows = real_load_card_rows(conn, names)
            rows.pop(COMMANDER, None)
            return rows

        monkeypatch.setattr(forensics_module, "load_card_rows", _without_commander)
        report = compute_forensics(
            db_path=forensics_paths["db"],
            tags_path=forensics_paths["tags"],
            fixture_path=forensics_paths["fixture"],
        )
        err = capsys.readouterr().err
        assert "has no cards row" in err
        assert COMMANDER in err
        # Classification proceeded — the entry is still produced.
        assert [e.commander for e in report.entries] == [COMMANDER]


class TestPreconditions:
    def test_tensor_config_hash_mismatch_raises(self, forensics_paths: dict[str, Path]) -> None:
        """Stale tensor (no rows at the live hash) → precondition error,
        nothing computed."""
        conn = open_db(forensics_paths["db"])
        try:
            conn.execute("UPDATE rule_contributions SET config_hash = 'stale-hash'")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(ForensicsPreconditionError, match=r"config_hash.*--repin"):
            compute_forensics(
                db_path=forensics_paths["db"],
                tags_path=forensics_paths["tags"],
                fixture_path=forensics_paths["fixture"],
            )

    def test_missing_tags_db_raises_with_hint(self, forensics_paths: dict[str, Path], tmp_path: Path) -> None:
        with pytest.raises(ForensicsPreconditionError, match="not found"):
            compute_forensics(
                db_path=forensics_paths["db"],
                tags_path=tmp_path / "missing_tags.db",
                fixture_path=forensics_paths["fixture"],
            )

    def test_missing_fixture_raises(self, forensics_paths: dict[str, Path], tmp_path: Path) -> None:
        with pytest.raises(ForensicsPreconditionError, match=r"fixture.*not found"):
            compute_forensics(
                db_path=forensics_paths["db"],
                tags_path=forensics_paths["tags"],
                fixture_path=tmp_path / "missing_fixture.json",
            )

    def test_missing_synergy_db_raises(self, forensics_paths: dict[str, Path], tmp_path: Path) -> None:
        with pytest.raises(ForensicsPreconditionError, match="import_cardsfolder"):
            compute_forensics(
                db_path=tmp_path / "missing_synergy.db",
                tags_path=forensics_paths["tags"],
                fixture_path=forensics_paths["fixture"],
            )

    def test_partner_fixture_entry_raises(self, forensics_paths: dict[str, Path]) -> None:
        _write_fixture(forensics_paths["fixture"], [["Tymna the Weaver", "Thrasios, Triton Hero"]])
        with pytest.raises(ForensicsPreconditionError, match="partners not supported in v1"):
            compute_forensics(
                db_path=forensics_paths["db"],
                tags_path=forensics_paths["tags"],
                fixture_path=forensics_paths["fixture"],
            )

    def test_duplicate_commander_fixture_entry_raises(self, forensics_paths: dict[str, Path]) -> None:
        """Duplicate fixture commanders would silently double-count every
        aggregate — fail loud (the partner-entry pattern)."""
        _write_fixture(forensics_paths["fixture"], [COMMANDER, COMMANDER])
        with pytest.raises(ForensicsPreconditionError, match="more than once"):
            compute_forensics(
                db_path=forensics_paths["db"],
                tags_path=forensics_paths["tags"],
                fixture_path=forensics_paths["fixture"],
            )

    def test_empty_fixture_raises(self, forensics_paths: dict[str, Path]) -> None:
        _write_fixture(forensics_paths["fixture"], [])
        with pytest.raises(ForensicsPreconditionError, match="no entries"):
            compute_forensics(
                db_path=forensics_paths["db"],
                tags_path=forensics_paths["tags"],
                fixture_path=forensics_paths["fixture"],
            )


# ---------------------------------------------------------------------------
# Sentinel imports — never duplicated
# ---------------------------------------------------------------------------


def test_unranked_sentinel_is_imported_from_engine() -> None:
    """The forensics module must reuse the engine's sentinel object,
    not redefine its value locally."""
    from mtg_synergy_graph.bench import forensics

    assert forensics.UNRANKED_EDHREC_SENTINEL is UNRANKED_EDHREC_SENTINEL
