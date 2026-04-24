"""Tests for ``scripts/forge_oracle.py propose-rules``.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 8.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

# scripts/ is on sys.path via tests/conftest.py
import forge_oracle as fo_cli
import gap_report as gr
import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.forge_oracle import config as fo_config

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_forge_oracle_db(tmp_path: Path) -> Path:
    """Build a minimal forge_oracle.db with a current config hash."""
    path = tmp_path / "forge_oracle.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE forge_precon_ppmi ("
        "  port_signature_a TEXT, port_signature_b TEXT,"
        "  ppmi REAL, decks_count INTEGER, last_updated TEXT,"
        "  PRIMARY KEY (port_signature_a, port_signature_b));"
        "CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    inputs = fo_config.get_oracle_config_inputs(ppmi_smoothing_k=0.0, min_decks_count=3)
    fo_config.write_oracle_config(conn, inputs)
    conn.commit()
    conn.close()
    return path


def _make_synergy_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO cards (name, oracle_id, card_types, types, supertypes, legal_commander) "
            "VALUES ('Example Cmdr', 'c1', 'Creature', 'Creature', 'Legendary', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# _render_proposals_markdown — delegates to generators
# ---------------------------------------------------------------------------


def _make_gap(template: str, impact: float = 10.0, forge_signal: float = 1.3) -> gr.RuleProposal:
    stat = gr.GapStat(
        signature=("effect", "Token", ""),
        commanders=round(impact),
        activations=0,
        exemplars=("CmdrA", "CmdrB"),
        top_rules=(),
    )
    stat = replace(stat, forge_signal=forge_signal)
    return gr.RuleProposal(
        gap=stat,
        template=template,
        rationale="test rationale",
        gate_sketch="(port) -> True",
        tier_sketches=(),
        pool_sizes={},
    )


def test_render_shows_registered_generator_preview() -> None:
    """When the proposal's template has a registered generator, the
    renderer embeds a scaffold-preview line and the "run" hint."""

    class FakeArtifacts:
        rule_id = "rid"
        helper_module_path = Path("helper.py")
        test_module_path = Path("test_helper.py")
        helper_module_src = ""
        test_module_src = ""
        bucket = "port_match"
        multiplier = 1.0

    gens = {"test_tpl": lambda prop: FakeArtifacts()}
    props = [_make_gap("test_tpl")]
    rendered = fo_cli._render_proposals_markdown(props, gens, stats_total=1, eligible_total=1)
    assert "Scaffold preview" in rendered
    assert "rule_id `rid`" in rendered
    assert "scripts/scaffold_rule.py --template test_tpl" in rendered


def test_render_handles_missing_generator_gracefully() -> None:
    """Proposals with no registered generator show as needs_template —
    no crash, no scaffold, clear label."""
    gens: dict = {}
    props = [_make_gap("nonexistent_template")]
    rendered = fo_cli._render_proposals_markdown(props, gens, stats_total=1, eligible_total=1)
    assert "no generator registered" in rendered
    assert "Scaffold preview" not in rendered


def test_render_handles_generator_exception_gracefully() -> None:
    """A raising generator doesn't break the whole report — error is inlined."""

    def boom(prop: gr.RuleProposal) -> object:
        raise ValueError("simulated generator bug")

    gens = {"bad_tpl": boom}
    props = [_make_gap("bad_tpl")]
    rendered = fo_cli._render_proposals_markdown(props, gens, stats_total=1, eligible_total=1)
    assert "generator raised" in rendered
    assert "simulated generator bug" in rendered


def test_render_header_reports_coverage_counts() -> None:
    """Summary lists total / with-generator / awaiting-template counts."""

    class FakeArtifacts:
        rule_id = "rid"
        helper_module_path = Path("h.py")
        test_module_path = Path("t.py")
        helper_module_src = ""
        test_module_src = ""
        bucket = "b"
        multiplier = 1.0

    gens = {"ok": lambda prop: FakeArtifacts()}
    props = [_make_gap("ok"), _make_gap("missing"), _make_gap("ok")]
    rendered = fo_cli._render_proposals_markdown(props, gens, stats_total=3, eligible_total=3)
    assert "With registered generator**: 2" in rendered
    assert "Awaiting new template**: 1" in rendered


def test_render_empty_proposals_returns_placeholder() -> None:
    rendered = fo_cli._render_proposals_markdown([], {}, stats_total=0, eligible_total=0)
    assert "No eligible gaps" in rendered


def test_render_shows_weighted_impact_and_forge_signal() -> None:
    gens: dict = {}
    props = [_make_gap("x", impact=10.0, forge_signal=1.4)]
    rendered = fo_cli._render_proposals_markdown(props, gens, stats_total=1, eligible_total=1)
    assert "forge_signal 1.40" in rendered
    assert "* forge_signal" in rendered


# ---------------------------------------------------------------------------
# CLI error paths — missing DB, stale hash
# ---------------------------------------------------------------------------


def test_cli_missing_forge_db_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    rc = fo_cli.main(
        [
            "propose-rules",
            "--synergy-db",
            str(synergy_db),
            "--forge-oracle-db",
            str(tmp_path / "nope.db"),
        ]
    )
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cli_stale_hash_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    forge_db = tmp_path / "forge_oracle.db"
    conn = sqlite3.connect(forge_db)
    conn.executescript(
        "CREATE TABLE forge_precon_ppmi ("
        "  port_signature_a TEXT, port_signature_b TEXT,"
        "  ppmi REAL, decks_count INTEGER, last_updated TEXT,"
        "  PRIMARY KEY (port_signature_a, port_signature_b));"
        "CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    # Store hash built under k=2.0, then call CLI with default k=0.0 → mismatch.
    fo_config.write_oracle_config(
        conn,
        fo_config.get_oracle_config_inputs(ppmi_smoothing_k=2.0, min_decks_count=3),
    )
    conn.commit()
    conn.close()
    rc = fo_cli.main(
        [
            "propose-rules",
            "--synergy-db",
            str(synergy_db),
            "--forge-oracle-db",
            str(forge_db),
        ]
    )
    assert rc == 2


def test_cli_missing_synergy_db_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    forge_db = _make_forge_oracle_db(tmp_path)
    rc = fo_cli.main(
        [
            "propose-rules",
            "--synergy-db",
            str(tmp_path / "no_synergy.db"),
            "--forge-oracle-db",
            str(forge_db),
        ]
    )
    assert rc == 2
    assert "synergy" in capsys.readouterr().err.lower()


def test_cli_runs_end_to_end_empty_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Synergy DB has one legendary creature but no ports → zero gaps →
    renders placeholder, exits 0."""
    synergy_db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    rc = fo_cli.main(
        [
            "propose-rules",
            "--synergy-db",
            str(synergy_db),
            "--forge-oracle-db",
            str(forge_db),
            "--top",
            "5",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Forge-signal-ranked rule proposals" in out


def test_cli_top_zero_renders_header_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    rc = fo_cli.main(
        [
            "propose-rules",
            "--synergy-db",
            str(synergy_db),
            "--forge-oracle-db",
            str(forge_db),
            "--top",
            "0",
        ]
    )
    assert rc == 0
    assert "Forge-signal-ranked rule proposals" in capsys.readouterr().out


def test_cli_output_file(tmp_path: Path) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    out_path = tmp_path / "proposals.md"
    rc = fo_cli.main(
        [
            "propose-rules",
            "--synergy-db",
            str(synergy_db),
            "--forge-oracle-db",
            str(forge_db),
            "--output",
            str(out_path),
        ]
    )
    assert rc == 0
    assert out_path.is_file()
    assert "Forge-signal-ranked" in out_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# argv parsing
# ---------------------------------------------------------------------------


def test_help_mentions_propose_rules(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        fo_cli.main(["--help"])
    out = capsys.readouterr().out
    assert "propose-rules" in out
