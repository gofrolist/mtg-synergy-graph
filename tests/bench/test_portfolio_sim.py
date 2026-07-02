"""Portfolio-selection R0 harness tests (plan 2026-07-02-004, Unit 2).

Covers the shared score-decomposition helper + greedy assembler in
``mtg_synergy_graph.portfolio`` and the simulation instrument in
``mtg_synergy_graph.bench.portfolio_sim``:

* hand-computed greedy order on a synthetic 3-family setup;
* λ=0 identity limit against a synthetic production ordering;
* pool < 30 selects everything;
* all-one-family equal-shares order preservation + unequal-shares
  reordering (the share-arithmetic property);
* negative-net family passthrough / mass exclusion;
* named error for a missing commander (no silent skip);
* deterministic bootstrap bands under a fixed seed;
* decomposition-recomposition EXACTNESS (bitwise) against real scoring
  on a tmp-path DB built from the committed cardsfolder fixtures — the
  production total is the page-side ``to_legacy_buckets()["total"]``,
  which under flag-OFF defaults applies NO concentration dampener
  (that lives only in ``UniversalScore.score``).

DB discipline: every database is created under ``tmp_path`` /
``tmp_path_factory`` (conftest autouse guard fails the run otherwise).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.portfolio_sim import (
    CommanderResolutionError,
    bootstrap_band,
    build_commander_sim,
    load_fixture_commanders,
    main,
    resolve_trap_commanders,
    run_bands,
    run_share,
    run_sweep,
)
from mtg_synergy_graph.complement_rules import PortComplement
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.engine import SynergyEngine
from mtg_synergy_graph.importer import import_cards_folder
from mtg_synergy_graph.portfolio import (
    FAMILY_MAP,
    ScoreDecomposition,
    UnmappedRuleFamilyError,
    assemble_portfolio,
    decompose_universal_score,
)
from mtg_synergy_graph.universal_scorer import UniversalScore
from mtg_synergy_graph.validate import commander_to_slug

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
KORVOLD = "Korvold, Fae-Cursed King"


# ---------------------------------------------------------------------------
# Synthetic decomposition helpers
# ---------------------------------------------------------------------------


def _decomp(
    total: float,
    family_syn: dict[str, float],
    *,
    flat: dict[str, float] | None = None,
    anti: float = 0.0,
    residual: float = 0.0,
    dampener: float = 1.0,
    rank_bonus: float = 0.0,
) -> ScoreDecomposition:
    return ScoreDecomposition(
        family_syn=family_syn,
        family_syn_flat=flat if flat is not None else {},
        anti_total=anti,
        residual=residual,
        dampener=dampener,
        total=total,
        per_rule_net={},
        rank_bonus=rank_bonus,
    )


def _assemble(decomps: dict[str, ScoreDecomposition], **kwargs):
    kwargs.setdefault("lam", 0.0)
    kwargs.setdefault("cmc_lookup", {})
    kwargs.setdefault("rank_lookup", {})
    return assemble_portfolio(decomps, **kwargs)


# ---------------------------------------------------------------------------
# Greedy assembler — synthetic scenarios
# ---------------------------------------------------------------------------


def test_greedy_hand_computed_three_family_order() -> None:
    """Hand-walked greedy at λ=1 (exp): A floods f1, so B (same family)
    drops behind C and D despite a higher raw total."""
    import math

    decomps = {
        "A": _decomp(1.0, {"f1": 1.0}),
        "B": _decomp(0.9, {"f1": 0.9}),
        "C": _decomp(0.8, {"f2": 0.8}),
        "D": _decomp(0.7, {"f3": 0.7}),
    }
    result = _assemble(decomps, lam=1.0, k=4)
    assert result.cards == ("A", "C", "D", "B")
    # Effective scores at pick time (hand-computed).
    effs = {p.card: p.effective_score for p in result.picks}
    assert effs["A"] == pytest.approx(1.0)
    assert effs["C"] == pytest.approx(0.8)
    assert effs["D"] == pytest.approx(0.7)
    # B picked after f1 mass reached 1.0: 0.9 - 0.9 * (1 - e^-1).
    assert effs["B"] == pytest.approx(0.9 * math.exp(-1.0))
    assert dict(result.family_mass) == pytest.approx({"f1": 1.9, "f2": 0.8, "f3": 0.7})


def test_lambda_zero_reproduces_production_ordering() -> None:
    """λ=0 identity limit: effective == total bitwise, 4-key tiebreaks."""
    decomps = {
        "Zeta": _decomp(1.0, {"f": 1.0}),
        "Alpha": _decomp(1.0, {"f": 1.0}),  # same total+cmc+rank → name breaks
        "Mid": _decomp(0.5, {"g": 0.5}),
        "CheapMid": _decomp(0.5, {"g": 0.5}),  # same total, lower cmc wins
        "RankedMid": _decomp(0.5, {"g": 0.5}),  # same total+cmc, better rank
    }
    cmc = {"Zeta": 3.0, "Alpha": 3.0, "Mid": 2.0, "CheapMid": 1.0, "RankedMid": 2.0}
    rank = {"Zeta": 10, "Alpha": 10, "Mid": 500, "CheapMid": 500, "RankedMid": 100}
    expected = ["Alpha", "Zeta", "CheapMid", "RankedMid", "Mid"]
    for lam_zero_decay in ("exp", "harmonic"):
        result = _assemble(decomps, lam=0.0, decay=lam_zero_decay, k=5, cmc_lookup=cmc, rank_lookup=rank)
        assert list(result.cards) == expected
        # Identity limit is bitwise: eff == the exact production total.
        for pick in result.picks:
            assert pick.effective_score == decomps[pick.card].total


def test_pool_smaller_than_k_selects_all() -> None:
    decomps = {"A": _decomp(1.0, {"f": 1.0}), "B": _decomp(0.5, {"f": 0.5})}
    result = _assemble(decomps, lam=2.0, k=30)
    assert set(result.cards) == {"A", "B"}
    assert len(result.picks) == 2


def test_all_one_family_equal_shares_preserves_order() -> None:
    """Equal per-pick family shares → identical discount for every
    remaining card → raw-total order preserved at any λ."""
    decomps = {
        "A": _decomp(1.0, {"f": 0.5}),
        "B": _decomp(0.9, {"f": 0.5}),
        "C": _decomp(0.8, {"f": 0.5}),
        "D": _decomp(0.7, {"f": 0.5}),
    }
    result = _assemble(decomps, lam=2.0, k=4)
    assert result.cards == ("A", "B", "C", "D")


def test_all_one_family_unequal_shares_reorders() -> None:
    """A residual-heavy card overtakes a synergy-heavy one once the
    family floods (the pass-2 share-arithmetic property)."""
    decomps = {
        "A": _decomp(1.0, {"f": 1.0}),
        "C": _decomp(0.99, {"f": 0.99}),
        "B": _decomp(0.98, {"f": 0.02}),  # mostly residual
    }
    result = _assemble(decomps, lam=3.0, k=3)
    assert result.cards == ("A", "B", "C")


def test_negative_family_contribution_passthrough_and_mass_exclusion() -> None:
    """Negative family nets pass through undiscounted and never
    accumulate mass (clamp-at-zero, plan flow I8)."""
    decomps = {
        "X": _decomp(0.5, {"f": -0.4}),
        "Y": _decomp(0.45, {"f": 0.45}),
    }
    one = _assemble(decomps, lam=5.0, k=1)
    assert one.cards == ("X",)
    assert one.picks[0].effective_score == 0.5  # undiscounted passthrough
    assert dict(one.family_mass) == {}  # excluded from mass

    both = _assemble(decomps, lam=5.0, k=2)
    # Y sees NO discount from X's negative net.
    assert both.cards == ("X", "Y")
    assert both.picks[1].effective_score == 0.45
    assert dict(both.family_mass) == pytest.approx({"f": 0.45})


def test_flat_discount_base_uses_flat_share_only() -> None:
    """discount_base='flat' discounts (and accumulates) only the
    flat/density share; the non-flat share passes through."""
    decomps = {
        "A": _decomp(1.0, {"f": 1.0}, flat={"f": 1.0}),
        "B": _decomp(0.9, {"f": 0.9}, flat={}),  # non-flat synergy only
        "C": _decomp(0.85, {"f": 0.85}, flat={"f": 0.85}),
    }
    result = _assemble(decomps, lam=10.0, k=3, discount_base="flat")
    # B is untouched by the flat-only discount; C craters after A.
    assert result.cards == ("A", "B", "C")
    assert result.picks[1].effective_score == 0.9
    assert dict(result.family_mass) == pytest.approx({"f": 1.85})


def test_dampener_reapplied_to_discounted_sum() -> None:
    """The frozen dampener scales the discount correction."""
    import math

    decomps = {
        "A": _decomp(1.0, {"f": 1.0}),
        "B": _decomp(0.9, {"f": 0.8}, dampener=0.75),
    }
    result = _assemble(decomps, lam=1.0, k=2)
    # eff(B) = total - dampener * syn_f * (1 - e^{-1·1.0})
    expected = 0.9 - 0.75 * 0.8 * (1.0 - math.exp(-1.0))
    assert result.picks[1].effective_score == pytest.approx(expected)


def test_assembler_input_validation() -> None:
    decomps = {"A": _decomp(1.0, {"f": 1.0})}
    with pytest.raises(ValueError, match="lam must be >= 0"):
        _assemble(decomps, lam=-0.1)
    with pytest.raises(ValueError, match="k must be > 0"):
        _assemble(decomps, k=0)
    with pytest.raises(ValueError, match="unknown decay form"):
        _assemble(decomps, decay="cubic")
    with pytest.raises(ValueError, match="discount_base"):
        _assemble(decomps, discount_base="everything")


# ---------------------------------------------------------------------------
# Decomposition — hand-built UniversalScore
# ---------------------------------------------------------------------------


def _pc(
    rule_id: str,
    direction: str = "synergy",
    cmdr_event: str = "E1",
    cand_event: str = "F1",
    filter_group: str = "",
) -> PortComplement:
    return PortComplement(
        rule_id=rule_id,
        direction=direction,  # type: ignore[arg-type]
        candidate="Cand",
        cmdr_event=cmdr_event,
        cand_event=cand_event,
        filter_group=filter_group,
    )


def _hand_built_score() -> UniversalScore:
    complements = [
        _pc("lord"),
        _pc("lord"),  # duplicate key — must dedup to one contribution
        _pc("tribal_density"),
        _pc("trigger_effect"),
        _pc("replacement_blocks", direction="anti_synergy"),
    ]
    idf = {
        ("lord", "E1", "F1", ""): 0.5,
        ("tribal_density", "E1", "F1", ""): 0.2,
        ("trigger_effect", "E1", "F1", ""): 0.3,
        ("replacement_blocks", "E1", "F1", ""): 0.15,
    }
    return UniversalScore(
        complements=complements,
        staple_bonus=0.01,
        idf_weights=idf,
        circuit_bonus=0.05,
        cmc_bonus=0.007,
        rank_bonus=0.004,
        embedding_contribution=0.0,
    )


def test_decompose_recomposes_production_total_bitwise() -> None:
    us = _hand_built_score()
    d = decompose_universal_score(us, FAMILY_MAP)
    assert d.total == us.to_legacy_buckets()["total"]
    # Family grouping honors the R6 merges: lord + tribal_density → tribal.
    assert dict(d.family_syn) == pytest.approx({"tribal": 0.7, "trigger_effect": 0.3})
    # Flat share: tribal_density is a flat/density rule; lord is not.
    assert dict(d.family_syn_flat) == pytest.approx({"tribal": 0.2})
    assert d.anti_total == pytest.approx(0.15)
    # residual = staple 0.01 + breadth 0.04 (3 rules) + pair 0.03
    # (lord+tribal_density) + circuit/cmc/rank 0.061 + embedding 0.
    assert d.residual == pytest.approx(0.141)
    assert d.rank_bonus == 0.004
    assert dict(d.per_rule_net) == pytest.approx(
        {"lord": 0.5, "tribal_density": 0.2, "trigger_effect": 0.3, "replacement_blocks": -0.15}
    )
    # Algebraic recomposition (λ=0 form) matches up to float reassociation.
    recomposed = d.dampener * sum(d.family_syn.values()) - d.anti_total + d.residual
    assert recomposed == pytest.approx(d.total, abs=1e-12)


def test_decompose_dampener_is_production_semantics_under_flag_off() -> None:
    """Under flag-OFF defaults the PRODUCTION total applies no
    concentration dampener (it lives only in ``score``), so the
    decomposition's frozen factor is 1.0 even for >70%-concentrated
    multi-rule candidates — and ``total`` matches ``to_legacy_buckets``
    (NOT ``score``, which is dampened here)."""
    complements = [_pc("lord"), _pc("trigger_effect")]
    idf = {
        ("lord", "E1", "F1", ""): 0.8,
        ("trigger_effect", "E1", "F1", ""): 0.2,
    }
    us = UniversalScore(complements=complements, idf_weights=idf)
    d = decompose_universal_score(us, FAMILY_MAP)
    assert d.dampener == 1.0
    buckets_total = us.to_legacy_buckets()["total"]
    assert d.total == buckets_total
    # score() dampens the 80%-concentrated mix; the page total does not.
    assert us.score != buckets_total
    assert us.score < buckets_total


def test_decompose_raises_named_error_for_unmapped_rule() -> None:
    us = UniversalScore(
        complements=[_pc("totally_unknown_rule_xyz")],
        idf_weights={("totally_unknown_rule_xyz", "E1", "F1", ""): 0.5},
    )
    with pytest.raises(UnmappedRuleFamilyError, match="totally_unknown_rule_xyz"):
        decompose_universal_score(us, FAMILY_MAP)


def test_decompose_raises_named_error_for_unmapped_anti_rule() -> None:
    us = UniversalScore(
        complements=[_pc("lord"), _pc("unknown_anti_rule", direction="anti_synergy")],
        idf_weights={("lord", "E1", "F1", ""): 0.5},
    )
    with pytest.raises(UnmappedRuleFamilyError, match="unknown_anti_rule"):
        decompose_universal_score(us, FAMILY_MAP)


# ---------------------------------------------------------------------------
# Named error for a missing commander (no silent skip)
# ---------------------------------------------------------------------------


class _RaisingEngine:
    _candidate_cache = None

    def __init__(self) -> None:
        self._score_cache: dict = {}

    def page(self, commander, offset=0, limit=100):
        raise ValueError(f"unknown commander: {commander!r}")


class _EmptyPoolEngine:
    _candidate_cache = None

    def __init__(self) -> None:
        self._score_cache: dict = {}

    def page(self, commander, offset=0, limit=100):
        from types import SimpleNamespace

        return SimpleNamespace(items=[])


def test_missing_commander_raises_named_error() -> None:
    with pytest.raises(CommanderResolutionError, match="Nonexistent Commander"):
        build_commander_sim(_RaisingEngine(), None, "Nonexistent Commander")


def test_empty_pool_raises_named_error() -> None:
    with pytest.raises(CommanderResolutionError, match="empty scored pool"):
        build_commander_sim(_EmptyPoolEngine(), None, "Poolless Commander")


# ---------------------------------------------------------------------------
# Bootstrap bands — determinism under a fixed seed
# ---------------------------------------------------------------------------


def test_bootstrap_band_deterministic_under_fixed_seed() -> None:
    values = [0.1, 0.25, 0.3, 0.22, 0.19, 0.4, 0.05, 0.33]
    a = bootstrap_band(values, seed=17, n_boot=500)
    b = bootstrap_band(values, seed=17, n_boot=500)
    assert a == b
    c = bootstrap_band(values, seed=18, n_boot=500)
    assert c != a
    assert a["ci95_low"] <= a["mean"] <= a["ci95_high"]
    assert a["half_width"] > 0


def test_bootstrap_band_empty_values() -> None:
    band = bootstrap_band([], seed=1, n_boot=10)
    assert band["n"] == 0
    assert band["half_width"] == 0.0


def test_resolve_trap_commanders() -> None:
    commanders = ["Krenko, Mob Boss", "Kess, Dissident Mage", "Korvold, Fae-Cursed King"]
    resolved, absent = resolve_trap_commanders(commanders)
    assert resolved["Krenko"] == "Krenko, Mob Boss"
    assert resolved["Kess"] == "Kess, Dissident Mage"
    assert "Magda" in absent and "Ghoulcaller Gisa" in absent


# ---------------------------------------------------------------------------
# Real-scoring integration on a tmp-path DB (cardsfolder fixtures)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("portfolio_sim") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    conn.close()
    return db_path


@pytest.fixture()
def engine(engine_db: Path):
    eng = SynergyEngine(engine_db)
    yield eng
    eng.close()


@pytest.fixture(scope="module")
def edhrec_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Tiny EDHREC tags DB with High Synergy rows for Korvold."""
    path = tmp_path_factory.mktemp("portfolio_sim_edhrec") / "tags.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE edhrec_card_synergy ("
        "commander_slug TEXT, card_name TEXT, synergy REAL, "
        "inclusion INTEGER, num_decks INTEGER, section TEXT)"
    )
    slug = commander_to_slug(KORVOLD)
    rows = [
        (slug, "Bloodghast", 0.9, 50, 100, "High Synergy Cards"),
        (slug, "Phyrexian Altar", 0.8, 40, 100, "High Synergy Cards"),
        (slug, "Imaginary Payoff", 0.7, 30, 100, "High Synergy Cards"),
        (slug, "Dockside Extortionist", 0.6, 60, 100, "Top Cards"),
    ]
    conn.executemany("INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def edhrec_conn(edhrec_db: Path):
    conn = sqlite3.connect(edhrec_db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_real_scoring_recomposition_is_bitwise_exact(engine: SynergyEngine) -> None:
    """CONTRACT: decompose + recompose at λ=0 reproduces every pool
    card's production total bitwise, and the λ=0 assembly equals the
    engine's own page() top-30. build_commander_sim enforces both
    internally (SelfCheckError); this test re-derives them explicitly
    against a fresh page() pass."""
    sim = build_commander_sim(engine, None, KORVOLD, granularities=("committed", "identity"))
    page = engine.page(KORVOLD, offset=0, limit=1_000_000)
    assert sim.pool_order == tuple(rec.card for rec in page.items)
    for rec in page.items:
        for gran in ("committed", "identity"):
            assert sim.decomps[gran][rec.card].total == rec.total_score
    # λ=0 assembly == production top-30 (pool here is < 30 → everything).
    result = assemble_portfolio(
        dict(sim.decomps["committed"]),
        lam=0.0,
        k=30,
        cmc_lookup=dict(sim.cmc_lookup),
        rank_lookup=dict(sim.rank_lookup),
    )
    assert result.cards == sim.base_top_30
    # Flag-OFF defaults: every production dampener factor is 1.0.
    assert all(d.dampener == 1.0 for d in sim.decomps["committed"].values())


def test_run_share_end_to_end(engine: SynergyEngine, edhrec_conn: sqlite3.Connection) -> None:
    report = run_share(
        engine,
        edhrec_conn,
        [KORVOLD],
        lam=0.5,
        decay="exp",
        discount_base="full",
        fixture="synthetic",
    )
    assert report["n_commanders"] == 1
    row = report["per_commander"][0]
    assert row["commander"] == KORVOLD
    # The fixture pool is smaller than 30, so the page top-30 IS the
    # pool — no recoverable misses inside the pool.
    assert row["n_misses"] == 0
    assert report["total_recovered"] <= report["total_misses"]


def test_run_bands_end_to_end_and_deterministic(engine: SynergyEngine, edhrec_conn: sqlite3.Connection) -> None:
    a = run_bands(engine, edhrec_conn, [KORVOLD], seed=17, n_boot=50, fixture="synthetic")
    b = run_bands(engine, edhrec_conn, [KORVOLD], seed=17, n_boot=50, fixture="synthetic")
    assert a == b
    assert a["n_commanders"] == 1
    assert a["ndcg_band"]["n"] == 1
    row = a["per_commander"][0]
    assert row["commander"] == KORVOLD
    assert row["gem_rate"] is not None  # EDHREC rows exist for Korvold
    overall = a["r9a_baseline"]["overall"]
    assert overall["n_pass"] <= overall["n_evaluable"]


def test_run_sweep_end_to_end(engine: SynergyEngine, edhrec_conn: sqlite3.Connection) -> None:
    report = run_sweep(
        engine,
        edhrec_conn,
        [KORVOLD],
        forms=["exp"],
        lams=[0.5],
        granularities=["committed", "identity"],
        bases=["full", "flat"],
        fixture="synthetic",
    )
    assert report["n_commanders"] == 1
    assert len(report["cells"]) == 4  # 1 form × 1 λ × 2 granularities × 2 bases
    for cell in report["cells"]:
        assert cell["n_ndcg"] == 1
        assert isinstance(cell["mean_ndcg_delta"], float)
        assert cell["cliff_count"] >= 0
        assert cell["rank_bonus_ablated_mean_delta"] is not None
        assert set(cell["r9a_gained_pass_rate"]) == {"monoculture", "other"}
    # None of the 12 trap fragments live in this synthetic fixture.
    assert len(report["trap_fragments_absent"]) == 12
    assert report["base_mean_cv_top30"] is not None


def test_main_share_smoke(
    engine_db: Path,
    edhrec_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "golden.json"
    fixture.write_text(
        json.dumps({"schema_version": 2, "config_hash": "", "created_at": "", "entries": [{"commander": KORVOLD}]}),
        encoding="utf-8",
    )
    outdir = tmp_path / "out"
    rc = main(
        [
            "share",
            "--db",
            str(engine_db),
            "--edhrec-db",
            str(edhrec_db),
            "--fixture",
            str(fixture),
            "--output",
            str(outdir),
            "--commanders",
            "1",
        ]
    )
    assert rc == 0
    report = json.loads((outdir / "share.json").read_text(encoding="utf-8"))
    assert report["n_commanders"] == 1
    out = capsys.readouterr().out
    assert "share" in out


def test_main_missing_db_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["share", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_load_fixture_commanders_reads_names(tmp_path: Path) -> None:
    fixture = tmp_path / "golden.json"
    fixture.write_text(
        json.dumps({"entries": [{"commander": "A"}, {"commander": "B"}]}),
        encoding="utf-8",
    )
    assert load_fixture_commanders(fixture) == ["A", "B"]
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"entries": []}), encoding="utf-8")
    with pytest.raises(Exception, match="no commander entries"):
        load_fixture_commanders(empty)
