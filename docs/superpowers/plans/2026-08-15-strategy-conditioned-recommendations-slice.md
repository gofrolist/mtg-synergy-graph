# Strategy-Conditioned Recommendations — Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `mtg-strategy-graph` — a new repo that recommends EDH cards conditioned on a user-selected strategy, where a rule added for one strategy provably cannot change another strategy's output.

**Architecture:** Forge DSL card scripts are parsed into typed "ports" (the ETL layer, moved verbatim from `mtg-synergy-graph`). A strategy is a data row carrying an availability predicate, a candidate signature, and a **rule manifest**. Scoring is deny-by-default: only rules in `manifest(strategy) ∪ core` are loaded, and IDF is computed over the strategy-local pool. Strategy signatures are mined offline from EDHREC theme-page inclusion rates; at inference nothing but Forge ports is consulted.

**Tech Stack:** Python 3.13, stdlib `sqlite3`, `uv` + `uv_build`, `pytest` (+`pytest-cov`, `pytest-xdist`), `ruff`, `pyright`. No numpy in the slice. No network at inference.

**Spec:** `docs/superpowers/specs/2026-08-14-strategy-conditioned-recommendations-design.md` (in the `mtg-synergy-graph` repo). Read it before starting; this plan implements §8's slice scope.

## Global Constraints

- **New repo path:** `/Users/evgenii.vasilenko/gofrolist/mtg-strategy-graph`. The old repo `/Users/evgenii.vasilenko/gofrolist/mtg-synergy-graph` stays on disk read-only for the whole slice — it is the parity reference (Task 4) and the baseline source (Task 9). Do not modify it except where a task says so explicitly.
- **Package name:** `mtg_strategy_graph`. **License:** GPL-3.0-only (inherited from Forge, whose DSL and card data this derives from).
- **Python:** `requires-python = ">=3.13,<3.15"`.
- **No inference-time EDHREC.** `themes.db` may only be opened by modules under `src/mtg_strategy_graph/labels/` and `src/mtg_strategy_graph/mining/`. Task 13 enforces this with a test.
- **Deny-by-default is absolute.** A rule not in `manifest(strategy) ∪ core` is never loaded, never summed, and never contributes to an IDF denominator.
- **SQL fragment interpolation** must be guarded by an explicit allowlist frozenset plus `raise ValueError` — never `assert` (stripped under `python -O`).
- **Tests must never pass a project-relative DB path** (`db="data/ports.db"`) to code that may call `open_db`. Use `tmp_path`. A session-scoped autouse fixture (Task 1) fails the run if a stray `*.db` appears at the repo root or in `data/`.
- **Network access** is allowed only in `labels/fetch.py` (Task 6), only through its paced client, and every response is cached to disk before parsing.
- **Every task ends with a commit.** Run `uv run pytest` before each commit; it must be green.

---

## File Structure

```
mtg-strategy-graph/
  pyproject.toml
  README.md
  LICENSE                                  GPL-3.0-only
  conftest.py                              stray-DB guard fixture
  src/mtg_strategy_graph/
    __init__.py                            public API: recommend(), __version__
    db.py                                  open_db(); ports schema only
    schema.sql                             cards, card_ports, port_attributes,
                                           card_svars, card_hints  (ETL only)
    etl/
      __init__.py
      parser.py                            Forge .txt → dicts        (moved)
      ports.py                             dicts → port rows         (moved)
      attributes.py                        valid_filter explosion    (moved)
      tokens.py                            TokenScript parsing       (moved)
      etb_replacement.py                   ETB replacement walk      (moved)
      copy_face_from.py                    DFC face resolution       (moved)
      importer.py                          cardsfolder → ports.db    (moved)
    labels/
      __init__.py
      schema.sql                           themes.db DDL
      fetch.py                             paced EDHREC client + cache
      store.py                             parse payload → themes.db rows
      resolve.py                           EDHREC card name → cards.name
      metrics.py                           inclusion, lift, core/discriminative sets
    strategy/
      __init__.py
      catalog.py                           load + validate strategies.json
      predicates.py                        predicate DSL → SQL + Python gate
      interpreter.py                       rule rows → candidate matches
      scoring.py                           deny-by-default scorer, strategy-local IDF
    mining/
      __init__.py
      miner.py                             within-commander contrast, log-odds
      guard.py                             anti-whitelist generalisation guard
    eval/
      __init__.py
      report.py                            judgment report + metric table
    data/
      strategies.json                      committed strategy catalog + manifests
    cli.py                                 recommend / mine / label / evaluate
  tests/…                                  mirrors src layout
  docs/
    baseline.json                          Task 9 output; pre-registered floors
    DECISIONS.md                           kill-criteria checkpoint records
```

---

### Task 1: Repo skeleton, tooling, stray-DB guard

**Files:**
- Create: `pyproject.toml`, `README.md`, `LICENSE`, `.gitignore`, `conftest.py`
- Create: `src/mtg_strategy_graph/__init__.py`
- Test: `tests/test_packaging.py`

**Interfaces:**
- Consumes: nothing.
- Produces: package `mtg_strategy_graph` with `__version__: str`; pytest fixture `_no_stray_db` (autouse, session scope).

- [ ] **Step 1: Create the repo and copy the licence**

```bash
mkdir -p ~/gofrolist/mtg-strategy-graph/{src/mtg_strategy_graph,tests,docs,data}
cd ~/gofrolist/mtg-strategy-graph
git init
cp ~/gofrolist/mtg-synergy-graph/LICENSE .
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "mtg-strategy-graph"
version = "0.1.0"
description = "Strategy-conditioned EDH card recommendation from Forge DSL ports"
license = "GPL-3.0-only"
license-files = ["LICENSE"]
requires-python = ">=3.13,<3.15"
dependencies = []

[build-system]
requires = ["uv_build>=0.12.1,<0.13"]
build-backend = "uv_build"

[dependency-groups]
dev = ["pytest>=8", "pytest-cov>=7", "pytest-xdist>=3", "ruff>=0.16", "pyright>=1.1.400"]

[tool.ruff]
line-length = 110
target-version = "py313"

[tool.pyright]
pythonVersion = "3.13"
typeCheckingMode = "basic"
exclude = ["tests/"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-n=auto --cov=src/mtg_strategy_graph --cov-fail-under=80 -m 'not integration'"
filterwarnings = ["error::ResourceWarning"]
markers = [
    "integration: requires the Forge cardsfolder or a built ports.db; excluded by default",
    "network: performs live HTTP requests to EDHREC; excluded by default",
]
```

Note `-m 'not integration'` does not exclude `network`; Task 6 adds that to the marker expression.

- [ ] **Step 3: Write `src/mtg_strategy_graph/__init__.py`**

```python
"""Strategy-conditioned EDH card recommendation from Forge DSL ports."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
```

- [ ] **Step 4: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
.coverage
.pytest_cache/
data/*.db
data/forge/
.audit/
```

- [ ] **Step 5: Write the failing test**

`tests/test_packaging.py`:

```python
from pathlib import Path

import mtg_strategy_graph


def test_version_is_exposed():
    assert mtg_strategy_graph.__version__ == "0.1.0"


def test_stray_db_guard_is_registered(pytestconfig):
    """The autouse guard from conftest.py must be active for every test."""
    assert Path("conftest.py").exists()
```

- [ ] **Step 6: Run it and watch it fail**

Run: `uv run pytest tests/test_packaging.py -v -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_strategy_graph'` until `uv sync` installs the project.

- [ ] **Step 7: Sync and write the stray-DB guard**

```bash
uv sync
```

`conftest.py` at the repo root:

```python
"""Repo-wide pytest configuration.

The stray-DB guard exists because ``CREATE TABLE IF NOT EXISTS`` will
silently materialise an empty SQLite file at whatever relative path it
is handed. A test that passes ``db="data/ports.db"`` therefore poisons
every later test whose skip-guard keys off that file existing. The
guard turns that silent corruption into a loud failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WATCHED_DIRS = (Path("."), Path("data"))


def _db_snapshot() -> set[Path]:
    found: set[Path] = set()
    for d in _WATCHED_DIRS:
        if d.is_dir():
            found.update(p.resolve() for p in d.glob("*.db"))
    return found


@pytest.fixture(scope="session", autouse=True)
def _no_stray_db():
    before = _db_snapshot()
    yield
    new = _db_snapshot() - before
    if new:
        names = ", ".join(sorted(str(p) for p in new))
        pytest.fail(
            f"test run created stray database file(s): {names}. "
            "Pass a tmp_path-based db path instead of a project-relative one."
        )
```

- [ ] **Step 8: Run the tests and watch them pass**

Run: `uv run pytest -v`
Expected: 2 passed.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: repo skeleton, tooling, stray-DB guard"
```

---

### Task 2: Ports schema and `open_db`

**Files:**
- Create: `src/mtg_strategy_graph/schema.sql`, `src/mtg_strategy_graph/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: Task 1's package.
- Produces: `open_db(path: str | Path, *, create: bool = True) -> sqlite3.Connection`, `PORTS_TABLES: frozenset[str]`.

- [ ] **Step 1: Copy the schema and strip it to ETL tables**

```bash
cp ~/gofrolist/mtg-synergy-graph/src/mtg_synergy_graph/schema.sql \
   ~/gofrolist/mtg-strategy-graph/src/mtg_strategy_graph/schema.sql
```

Delete these table blocks and their indexes, keeping only `cards`, `card_ports`, `port_attributes`, `card_svars`, `card_hints`:

`synergy_edges`, `graph_cache`, `causal_neighbours`, `rule_contributions`, `event_match_map`, `cost_feeds_trigger`, `rules`, `card_embeddings`, `card_embeddings_config`.

Those belong to the retired scoring layer. `card_hints` stays: it is populated by the importer and dropping it would change importer behaviour, which Task 4's parity gate would then flag as a regression.

- [ ] **Step 2: Write the failing test**

`tests/test_db.py`:

```python
import sqlite3

import pytest

from mtg_strategy_graph.db import PORTS_TABLES, open_db


def test_open_db_creates_exactly_the_ports_tables(tmp_path):
    conn = open_db(tmp_path / "ports.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert {r[0] for r in rows} == set(PORTS_TABLES)
    finally:
        conn.close()


def test_open_db_is_idempotent(tmp_path):
    p = tmp_path / "ports.db"
    open_db(p).close()
    conn = open_db(p)
    try:
        conn.execute("INSERT INTO cards (name) VALUES ('Test Card')")
        conn.commit()
    finally:
        conn.close()


def test_open_db_refuses_to_materialise_when_create_false(tmp_path):
    with pytest.raises(FileNotFoundError, match="run the importer"):
        open_db(tmp_path / "missing.db", create=False)
    assert not (tmp_path / "missing.db").exists()


def test_memory_path_allowed_even_with_create_false():
    conn = open_db(":memory:", create=False)
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_strategy_graph.db'`.

- [ ] **Step 4: Write `db.py`**

```python
"""SQLite helpers: open and initialise the ports schema.

Only the ETL tables live here. The retired scoring layer's tables
(rules, embeddings, tensors) are deliberately absent — a strategy's
rules are data rows in ``data/strategies.json``, not database state.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

#: Every table ``schema.sql`` creates. Asserted by the test suite so a
#: table added to the DDL without a deliberate decision fails loudly.
PORTS_TABLES: frozenset[str] = frozenset(
    {"cards", "card_ports", "port_attributes", "card_svars", "card_hints"}
)


def _schema_sql() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def open_db(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """Open a connection with the ports schema applied.

    ``create=False`` refuses to materialise a missing database, raising
    ``FileNotFoundError`` instead of silently producing a fully-schema'd
    empty DB. Read-side consumers pass ``create=False``; the importer
    keeps the creating default. ``:memory:`` is always allowed.
    """
    path_str = str(path)
    if not create and path_str != ":memory:" and not Path(path_str).exists():
        raise FileNotFoundError(
            f"SQLite database not found: {path_str} — run the importer "
            "(`uv run mtg-strategy-graph import-cards`) to build it."
        )
    conn = sqlite3.connect(path_str, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_schema_sql())
    return conn
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(db): ports schema and open_db"
```

---

### Task 3: Move the ETL modules

**Files:**
- Create: `src/mtg_strategy_graph/etl/__init__.py`, `parser.py`, `ports.py`, `attributes.py`, `tokens.py`, `etb_replacement.py`, `copy_face_from.py`, `importer.py`
- Test: `tests/etl/test_parser.py`, `tests/etl/test_ports.py`, `tests/etl/test_ports_coverage.py`, `tests/etl/test_importer.py`

**Interfaces:**
- Consumes: `open_db` from Task 2.
- Produces: `etl.parser.parse_card_file`, `etl.ports.extract_all_ports`, `etl.importer.import_cards_folder(folder: Path, conn, scryfall_db: Path | None = None, limit: int | None = None) -> ImportSummary`.

The dependency graph is already clean — `ports` imports only `etb_replacement`, `parser`, `tokens`; `importer` imports only `attributes`, `copy_face_from`, `parser`, `ports`. Nothing reaches into the scoring layer, so this is a copy plus an import-path rewrite.

- [ ] **Step 1: Copy the modules and their tests**

```bash
cd ~/gofrolist/mtg-strategy-graph
mkdir -p src/mtg_strategy_graph/etl tests/etl
OLD=~/gofrolist/mtg-synergy-graph
for f in parser ports attributes tokens etb_replacement copy_face_from importer; do
  cp $OLD/src/mtg_synergy_graph/$f.py src/mtg_strategy_graph/etl/$f.py
done
for f in test_parser test_ports test_ports_coverage test_importer; do
  cp $OLD/tests/$f.py tests/etl/$f.py
done
touch src/mtg_strategy_graph/etl/__init__.py tests/etl/__init__.py
```

- [ ] **Step 2: Rewrite the import paths**

In `src/mtg_strategy_graph/etl/*.py` the sibling imports (`from .parser import …`, `from .tokens import …`, `from .attributes import …`, `from .copy_face_from import …`, `from .ports import …`, `from .etb_replacement import …`) are already relative and stay correct inside the `etl` package. Only `importer.py` needs a change — its `open_db` usage now crosses a package boundary:

```bash
cd ~/gofrolist/mtg-strategy-graph
python3 - <<'PY'
import pathlib, re
for p in pathlib.Path("src/mtg_strategy_graph/etl").glob("*.py"):
    s = p.read_text()
    s = s.replace("from .db import", "from ..db import")
    s = s.replace("from mtg_synergy_graph", "from mtg_strategy_graph")
    p.write_text(s)
for p in pathlib.Path("tests/etl").glob("*.py"):
    s = p.read_text()
    s = s.replace("from mtg_synergy_graph.db", "from mtg_strategy_graph.db")
    s = re.sub(r"from mtg_synergy_graph\.(parser|ports|attributes|tokens|etb_replacement|copy_face_from|importer)",
               r"from mtg_strategy_graph.etl.\1", s)
    s = s.replace("from mtg_synergy_graph", "from mtg_strategy_graph")
    p.write_text(s)
PY
```

- [ ] **Step 3: Write `src/mtg_strategy_graph/etl/__init__.py`**

```python
"""Forge DSL extraction: card scripts in, typed port rows out.

Moved verbatim from ``mtg-synergy-graph``. Task 4's parity gate is the
contract that this move changed nothing: the same cardsfolder must
produce byte-identical port counts and the same
``(port_type, event_class)`` histogram.
"""

from __future__ import annotations
```

- [ ] **Step 4: Run the moved tests and fix what breaks**

Run: `uv run pytest tests/etl -v`
Expected: initially some failures from imports the copy pulled in that don't exist here (anything importing `heuristics`, `graph_engine`, `port_graph`, `complement_rules`, or `validate`). For each failure, delete the offending test **only if** it tests a scoring concern; if it tests parsing or port extraction, port the missing helper into `tests/etl/_helpers.py`. Record every deleted test in the commit message — a silently dropped test is how a parity regression hides.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: all green. Coverage may dip below 80% because `importer.py` is largely exercised by integration tests; if `--cov-fail-under` trips, that is expected here and Task 4 restores it. Temporarily run with `--cov-fail-under=0` to confirm the tests themselves pass, then leave the threshold alone.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(etl): move Forge parser and port extraction from mtg-synergy-graph

Verbatim move; import paths rewritten to the etl subpackage. Parity is
asserted separately in the next task."
```

---

### Task 4: Import CLI and the parity gate

**Files:**
- Create: `src/mtg_strategy_graph/cli.py`
- Modify: `pyproject.toml` (add `[project.scripts]`)
- Test: `tests/etl/test_parity.py`

**Interfaces:**
- Consumes: `etl.importer.import_cards_folder`, `db.open_db`.
- Produces: console script `mtg-strategy-graph` with subcommand `import-cards`; `cli.main(argv: list[str] | None = None) -> int`.

This is the gate that makes the move trustworthy. Without it, a subtle parser regression silently changes every mined signature downstream and there is no way to notice.

- [ ] **Step 1: Add the console script to `pyproject.toml`**

```toml
[project.scripts]
mtg-strategy-graph = "mtg_strategy_graph.cli:main"
```

- [ ] **Step 2: Write `cli.py` with the `import-cards` subcommand**

```python
"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .db import open_db
from .etl.importer import import_cards_folder


def _cmd_import_cards(args: argparse.Namespace) -> int:
    if not args.folder.exists():
        print(f"error: {args.folder} does not exist", file=sys.stderr)
        return 2
    conn = open_db(args.db)
    try:
        summary = import_cards_folder(
            args.folder, conn, scryfall_db=args.scryfall_db, limit=args.limit
        )
        conn.commit()
    finally:
        conn.close()
    print(f"imported {summary.cards} cards, {summary.ports} ports -> {args.db}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="mtg-strategy-graph", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-cards", help="Import a Forge cardsfolder into ports.db")
    p.add_argument("--folder", type=Path, default=Path("data/forge/forge-gui/res/cardsfolder"))
    p.add_argument("--db", type=Path, default=Path("data/ports.db"))
    p.add_argument("--scryfall-db", type=Path, default=Path("data/tags.db"))
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=_cmd_import_cards)

    args = parser.parse_args(argv)
    return int(args.func(args))
```

If `import_cards_folder`'s real signature or its summary's attribute names differ from `summary.cards` / `summary.ports`, read the moved `etl/importer.py` and use the real ones — do not adapt the importer to this call site.

- [ ] **Step 3: Write the failing parity test**

`tests/etl/test_parity.py`:

```python
"""Parity gate: the moved ETL must reproduce the old repo's extraction exactly.

Marked ``integration`` because it needs both the Forge cardsfolder and
the old repo's built ``synergy.db``. Run explicitly:

    uv run pytest -m integration tests/etl/test_parity.py -v
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

REFERENCE_DB = Path(
    os.environ.get("PARITY_REFERENCE_DB", str(Path.home() / "gofrolist/mtg-synergy-graph/data/synergy.db"))
)
NEW_DB = Path(os.environ.get("PARITY_NEW_DB", "data/ports.db"))

EXPECTED_CARDS = 32_327
EXPECTED_PORTS = 108_644


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dbs():
    if not REFERENCE_DB.exists():
        pytest.skip(f"reference DB missing: {REFERENCE_DB}")
    if not NEW_DB.exists():
        pytest.skip(f"new DB missing: {NEW_DB} — run `mtg-strategy-graph import-cards` first")
    return REFERENCE_DB, NEW_DB


def test_absolute_counts(dbs):
    _, new = dbs
    cards = _rows(new, "SELECT COUNT(*) FROM cards")[0][0]
    ports = _rows(new, "SELECT COUNT(*) FROM card_ports")[0][0]
    assert (cards, ports) == (EXPECTED_CARDS, EXPECTED_PORTS)


def test_port_type_event_class_histogram_matches_reference(dbs):
    ref, new = dbs
    sql = (
        "SELECT port_type, event_class, COUNT(*) FROM card_ports "
        "GROUP BY port_type, event_class ORDER BY port_type, event_class"
    )
    assert _rows(new, sql) == _rows(ref, sql)


def test_port_attribute_histogram_matches_reference(dbs):
    ref, new = dbs
    sql = (
        "SELECT attr_kind, COUNT(*) FROM port_attributes "
        "GROUP BY attr_kind ORDER BY attr_kind"
    )
    assert _rows(new, sql) == _rows(ref, sql)
```

- [ ] **Step 4: Build the DB and run the gate**

```bash
mkdir -p data
ln -s ~/gofrolist/mtg-synergy-graph/data/forge data/forge
ln -s ~/gofrolist/mtg-synergy-graph/data/tags.db data/tags.db
uv run mtg-strategy-graph import-cards
uv run pytest -m integration tests/etl/test_parity.py -v
```

Expected: 3 passed. If the histogram test fails, diff the two histograms and fix the **moved code** until it matches — never adjust `EXPECTED_*` to whatever came out. A mismatch here means the move lost something.

- [ ] **Step 5: Run the default suite**

Run: `uv run pytest -v`
Expected: green, and coverage back above 80% now that `cli.py` and the importer are exercised.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(cli): import-cards subcommand + ETL parity gate

Asserts 32,624 cards / 110,366 ports (re-verified 2026-08-15) and an
identical (port_type, event_class) histogram against the old repo's synergy.db."
```

---

### Task 5: `themes.db` schema

**Files:**
- Create: `src/mtg_strategy_graph/labels/__init__.py`, `src/mtg_strategy_graph/labels/schema.sql`, `src/mtg_strategy_graph/labels/store.py`
- Test: `tests/labels/test_store_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `labels.store.open_themes_db(path, *, create=True) -> sqlite3.Connection`, `THEMES_TABLES: frozenset[str]`.

Kept in a physically separate database from `ports.db`. The design-time-only rule (spec §4.2) is structural: the recommendation path never opens this file, and Task 13 tests that.

- [ ] **Step 1: Write the failing test**

`tests/labels/test_store_schema.py`:

```python
from mtg_strategy_graph.labels.store import THEMES_TABLES, open_themes_db


def test_schema_creates_expected_tables(tmp_path):
    conn = open_themes_db(tmp_path / "themes.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert {r[0] for r in rows} == set(THEMES_TABLES)
    finally:
        conn.close()


def test_theme_cards_rejects_duplicate_rows(tmp_path):
    import sqlite3

    conn = open_themes_db(tmp_path / "themes.db")
    try:
        row = ("korvold-fae-cursed-king", "sacrifice", "Mayhem Devil", 0.49, 800, 1598, "highsynergycards")
        conn.execute(
            "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
            " num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)", row
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
                " num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)", row
            )
    finally:
        conn.close()
```

Add `import pytest` at the top of the file.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/labels/test_store_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_strategy_graph.labels'`.

- [ ] **Step 3: Write `labels/schema.sql`**

```sql
-- Design-time-only EDHREC label corpus. Never opened by the
-- recommendation path (enforced by tests/strategy/test_no_edhrec_at_inference.py).

CREATE TABLE IF NOT EXISTS commander_themes (
    commander_slug TEXT NOT NULL,
    theme_slug     TEXT NOT NULL,
    label          TEXT,
    deck_count     INTEGER,
    scraped_at     TEXT NOT NULL,
    PRIMARY KEY (commander_slug, theme_slug)
);

-- num_decks / potential_decks is the inclusion rate and IS the label
-- (spec §6.1). `section` is retained so the High Synergy set can be
-- reconstructed as a secondary sanity signal.
CREATE TABLE IF NOT EXISTS theme_cards (
    commander_slug  TEXT NOT NULL,
    theme_slug      TEXT NOT NULL,
    card_name       TEXT NOT NULL,
    synergy         REAL,
    num_decks       INTEGER,
    potential_decks INTEGER,
    section         TEXT NOT NULL,
    PRIMARY KEY (commander_slug, theme_slug, card_name, section)
);

CREATE INDEX IF NOT EXISTS idx_theme_cards_pair ON theme_cards(commander_slug, theme_slug);

CREATE TABLE IF NOT EXISTS tag_cards (
    theme_slug      TEXT NOT NULL,
    color_identity  TEXT NOT NULL,
    card_name       TEXT NOT NULL,
    synergy         REAL,
    num_decks       INTEGER,
    potential_decks INTEGER,
    section         TEXT NOT NULL,
    PRIMARY KEY (theme_slug, color_identity, card_name, section)
);

CREATE TABLE IF NOT EXISTS tag_commanders (
    theme_slug     TEXT NOT NULL,
    color_identity TEXT NOT NULL,
    commander_name TEXT NOT NULL,
    rank           INTEGER NOT NULL,
    PRIMARY KEY (theme_slug, color_identity, commander_name)
);

-- One row per fetch, so every label can be traced to a response.
CREATE TABLE IF NOT EXISTS provenance (
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    n_rows      INTEGER NOT NULL,
    PRIMARY KEY (url, fetched_at)
);
```

- [ ] **Step 4: Write `labels/store.py`**

```python
"""Storage for the design-time EDHREC label corpus."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

THEMES_TABLES: frozenset[str] = frozenset(
    {"commander_themes", "theme_cards", "tag_cards", "tag_commanders", "provenance"}
)


def open_themes_db(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    path_str = str(path)
    if not create and path_str != ":memory:" and not Path(path_str).exists():
        raise FileNotFoundError(
            f"themes database not found: {path_str} — run "
            "`mtg-strategy-graph fetch-labels` to build it."
        )
    conn = sqlite3.connect(path_str)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
    )
    return conn
```

Add `"labels/schema.sql"` and `"schema.sql"` to package data if `uv_build` does not include `.sql` files by default — verify with `uv build && python -c "import zipfile,glob; print(zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist())"` and add a `[tool.uv.build-backend] source-include` entry if either is missing.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/labels -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(labels): themes.db schema and store"
```

---

### Task 6: Paced EDHREC fetch client with on-disk cache

**Files:**
- Create: `src/mtg_strategy_graph/labels/fetch.py`
- Modify: `pyproject.toml` (add `network` to the default deselect)
- Test: `tests/labels/test_fetch.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `fetch.fetch_json(url: str, *, cache_dir: Path, min_interval: float = 0.35) -> tuple[dict, int]` returning `(payload, http_status)`; `fetch.COMMANDER_URL`, `THEME_URL`, `TAG_URL` format strings.

- [ ] **Step 1: Update the default marker expression in `pyproject.toml`**

```toml
addopts = "-n=auto --cov=src/mtg_strategy_graph --cov-fail-under=80 -m 'not integration and not network'"
```

- [ ] **Step 2: Write the failing test**

`tests/labels/test_fetch.py`:

```python
import json
import time

import pytest

from mtg_strategy_graph.labels import fetch


def test_cache_hit_avoids_second_request(tmp_path, monkeypatch):
    calls = []

    def fake_open(url, timeout):  # noqa: ARG001
        calls.append(url)
        return json.dumps({"ok": True}).encode(), 200

    monkeypatch.setattr(fetch, "_http_get", fake_open)
    url = "https://json.edhrec.com/pages/commanders/korvold-fae-cursed-king.json"

    first, status = fetch.fetch_json(url, cache_dir=tmp_path, min_interval=0.0)
    second, _ = fetch.fetch_json(url, cache_dir=tmp_path, min_interval=0.0)

    assert first == second == {"ok": True}
    assert status == 200
    assert len(calls) == 1, "second call must be served from the on-disk cache"


def test_pacing_sleeps_between_live_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "_http_get", lambda url, timeout: (b"{}", 200))
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", slept.append)

    fetch.fetch_json("https://example.test/a.json", cache_dir=tmp_path, min_interval=0.5)
    fetch.fetch_json("https://example.test/b.json", cache_dir=tmp_path, min_interval=0.5)

    assert slept, "a paced client must sleep between distinct live requests"


def test_non_200_raises_and_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "_http_get", lambda url, timeout: (b"", 403))
    url = "https://example.test/forbidden.json"
    with pytest.raises(fetch.FetchError, match="403"):
        fetch.fetch_json(url, cache_dir=tmp_path, min_interval=0.0)
    assert list(tmp_path.glob("*.json")) == []
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/labels/test_fetch.py -v`
Expected: FAIL — no module `mtg_strategy_graph.labels.fetch`.

- [ ] **Step 4: Write `fetch.py`**

```python
"""Paced, cached HTTP client for the EDHREC JSON endpoints.

This is the only module in the package permitted to touch the network,
and every response is written to the on-disk cache before it is parsed,
so a corpus build is resumable and a re-run costs zero requests.

Endpoint shapes were established by probe on 2026-08-15:

* commander and theme pages both expose
  ``container.json_dict.cardlists[].cardviews[]`` with ``synergy``,
  ``num_decks`` and ``potential_decks`` per card; sections cap at 50.
* tag pages additionally carry a ``topcommanders`` cardlist.
* per-card-type sub-pages and ``/pages/themes/<theme>.json`` return 403.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

COMMANDER_URL = "https://json.edhrec.com/pages/commanders/{slug}.json"
THEME_URL = "https://json.edhrec.com/pages/commanders/{slug}/{theme}.json"
TAG_URL = "https://json.edhrec.com/pages/tags/{theme}/{colors}.json"

_USER_AGENT = "mtg-strategy-graph/0.1 (design-time label corpus build)"
_last_request_at = 0.0


class FetchError(RuntimeError):
    """Raised for any non-200 response."""


def _http_get(url: str, timeout: float) -> tuple[bytes, int]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), int(response.status)
    except urllib.error.HTTPError as exc:
        return b"", int(exc.code)


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return cache_dir / f"{digest}.json"


def fetch_json(url: str, *, cache_dir: Path, min_interval: float = 0.35, timeout: float = 30.0) -> tuple[dict, int]:
    """Return ``(payload, http_status)``, serving from cache when present.

    ``min_interval`` is the minimum wall-clock gap between two live
    requests. Cache hits do not sleep.
    """
    global _last_request_at

    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(cache_dir, url)
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8")), 200

    if min_interval:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    body, status = _http_get(url, timeout)
    _last_request_at = time.monotonic()

    if status != 200:
        raise FetchError(f"{status} fetching {url}")

    cached.write_text(body.decode("utf-8"), encoding="utf-8")
    return json.loads(body), status
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/labels/test_fetch.py -v`
Expected: 3 passed.

- [ ] **Step 6: Add one live smoke test, marked `network`**

Append to `tests/labels/test_fetch.py`:

```python
@pytest.mark.network
def test_live_commander_page_has_inclusion_fields(tmp_path):
    payload, status = fetch.fetch_json(
        fetch.COMMANDER_URL.format(slug="korvold-fae-cursed-king"), cache_dir=tmp_path
    )
    assert status == 200
    cardlists = payload["container"]["json_dict"]["cardlists"]
    sample = cardlists[0]["cardviews"][0]
    assert {"name", "synergy", "num_decks", "potential_decks"} <= set(sample)
```

Run: `uv run pytest -m network tests/labels/test_fetch.py -v`
Expected: 1 passed. This is the canary for an upstream payload change.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(labels): paced, cached EDHREC fetch client"
```

---

### Task 7: Parse EDHREC payloads into `themes.db`

**Files:**
- Modify: `src/mtg_strategy_graph/labels/store.py`
- Modify: `src/mtg_strategy_graph/cli.py` (add `fetch-labels`)
- Test: `tests/labels/test_parse.py`

**Interfaces:**
- Consumes: `fetch.fetch_json`, `open_themes_db`.
- Produces: `store.parse_cardlists(payload: dict) -> list[CardRow]` where `CardRow` is a `NamedTuple(name: str, synergy: float | None, num_decks: int | None, potential_decks: int | None, section: str)`; `store.parse_taglinks(payload) -> list[ThemeRow]`; `store.parse_topcommanders(payload) -> list[str]`; `store.build_corpus(conn, commanders, themes, colors, *, cache_dir) -> int`.

- [ ] **Step 1: Write the failing test**

`tests/labels/test_parse.py`:

```python
from mtg_strategy_graph.labels import store

PAYLOAD = {
    "panels": {
        "taglinks": [
            {"slug": "sacrifice", "value": "Sacrifice", "count": 1598},
            {"slug": "reanimator", "value": "Reanimator", "count": 216},
            {"slug": "", "value": "broken", "count": 5},
        ]
    },
    "container": {
        "json_dict": {
            "cardlists": [
                {
                    "tag": "highsynergycards",
                    "header": "High Synergy Cards",
                    "cardviews": [
                        {"name": "Mayhem Devil", "synergy": 0.49, "num_decks": 800, "potential_decks": 1598},
                        {"name": "No Synergy Field", "num_decks": 10, "potential_decks": 100},
                    ],
                },
                {
                    "tag": "topcommanders",
                    "header": "Top Commanders",
                    "cardviews": [{"name": "Korvold, Fae-Cursed King"}, {"name": "Prossh, Skyraider of Kher"}],
                },
            ]
        }
    },
}


def test_parse_cardlists_keeps_inclusion_fields_and_section():
    rows = store.parse_cardlists(PAYLOAD)
    by_name = {r.name: r for r in rows if r.section != "topcommanders"}
    assert by_name["Mayhem Devil"].num_decks == 800
    assert by_name["Mayhem Devil"].potential_decks == 1598
    assert by_name["Mayhem Devil"].section == "highsynergycards"


def test_parse_cardlists_keeps_cards_without_synergy():
    """A missing `synergy` must not drop the row — inclusion is the label."""
    rows = {r.name for r in store.parse_cardlists(PAYLOAD)}
    assert "No Synergy Field" in rows


def test_parse_taglinks_skips_empty_slugs():
    themes = store.parse_taglinks(PAYLOAD)
    assert [t.theme_slug for t in themes] == ["sacrifice", "reanimator"]
    assert themes[0].deck_count == 1598


def test_parse_topcommanders_returns_ordered_names():
    assert store.parse_topcommanders(PAYLOAD) == [
        "Korvold, Fae-Cursed King",
        "Prossh, Skyraider of Kher",
    ]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/labels/test_parse.py -v`
Expected: FAIL — `AttributeError: module 'mtg_strategy_graph.labels.store' has no attribute 'parse_cardlists'`.

- [ ] **Step 3: Add the parsers to `store.py`**

```python
import datetime as _dt
from typing import Any, NamedTuple

from .fetch import COMMANDER_URL, TAG_URL, THEME_URL, fetch_json


class CardRow(NamedTuple):
    name: str
    synergy: float | None
    num_decks: int | None
    potential_decks: int | None
    section: str


class ThemeRow(NamedTuple):
    theme_slug: str
    label: str
    deck_count: int | None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_cardlists(payload: dict) -> list[CardRow]:
    """Flatten ``container.json_dict.cardlists`` into rows.

    Rows are kept even when ``synergy`` is absent: the label is the
    inclusion rate (``num_decks / potential_decks``), not synergy, so
    dropping synergy-less cards would silently shrink the corpus.
    """
    out: list[CardRow] = []
    cardlists = payload.get("container", {}).get("json_dict", {}).get("cardlists") or []
    for cardlist in cardlists:
        section = cardlist.get("tag") or cardlist.get("header") or "unknown"
        for view in cardlist.get("cardviews") or []:
            name = view.get("name")
            if not name:
                continue
            out.append(
                CardRow(
                    name=name,
                    synergy=_as_float(view.get("synergy")),
                    num_decks=_as_int(view.get("num_decks")),
                    potential_decks=_as_int(view.get("potential_decks")),
                    section=section,
                )
            )
    return out


def parse_taglinks(payload: dict) -> list[ThemeRow]:
    panels = payload.get("panels") or {}
    taglinks = panels.get("taglinks") or [] if isinstance(panels, dict) else []
    out: list[ThemeRow] = []
    for tag in taglinks:
        slug = (tag.get("slug") or "").strip()
        if not slug:
            continue
        out.append(ThemeRow(slug, tag.get("value") or slug, _as_int(tag.get("count"))))
    return out


def parse_topcommanders(payload: dict) -> list[str]:
    """Commander names from a tag page's ``topcommanders`` cardlist, in rank order."""
    return [r.name for r in parse_cardlists(payload) if r.section == "topcommanders"]
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/labels/test_parse.py -v`
Expected: 4 passed.

- [ ] **Step 5: Add `build_corpus` and the `fetch-labels` subcommand**

Append to `store.py`:

```python
def _record(conn, url: str, status: int, n_rows: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO provenance (url, fetched_at, http_status, n_rows) VALUES (?,?,?,?)",
        (url, _dt.datetime.now(_dt.UTC).isoformat(), status, n_rows),
    )


def build_corpus(
    conn,
    commander_slugs: list[str],
    theme_slugs: list[str],
    color_identities: list[str],
    *,
    cache_dir,
) -> int:
    """Populate themes.db for the given corpus. Returns rows written."""
    written = 0
    for slug in commander_slugs:
        url = COMMANDER_URL.format(slug=slug)
        payload, status = fetch_json(url, cache_dir=cache_dir)
        themes = parse_taglinks(payload)
        conn.executemany(
            "INSERT OR REPLACE INTO commander_themes"
            " (commander_slug, theme_slug, label, deck_count, scraped_at) VALUES (?,?,?,?,?)",
            [(slug, t.theme_slug, t.label, t.deck_count, _dt.datetime.now(_dt.UTC).isoformat()) for t in themes],
        )
        _record(conn, url, status, len(themes))
        written += len(themes)

        available = {t.theme_slug for t in themes}
        for theme in theme_slugs:
            if theme not in available:
                continue
            turl = THEME_URL.format(slug=slug, theme=theme)
            tpayload, tstatus = fetch_json(turl, cache_dir=cache_dir)
            rows = parse_cardlists(tpayload)
            conn.executemany(
                "INSERT OR REPLACE INTO theme_cards (commander_slug, theme_slug, card_name,"
                " synergy, num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)",
                [(slug, theme, r.name, r.synergy, r.num_decks, r.potential_decks, r.section) for r in rows],
            )
            _record(conn, turl, tstatus, len(rows))
            written += len(rows)

    for theme in theme_slugs:
        for colors in color_identities:
            url = TAG_URL.format(theme=theme, colors=colors)
            payload, status = fetch_json(url, cache_dir=cache_dir)
            rows = parse_cardlists(payload)
            conn.executemany(
                "INSERT OR REPLACE INTO tag_cards (theme_slug, color_identity, card_name,"
                " synergy, num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)",
                [(theme, colors, r.name, r.synergy, r.num_decks, r.potential_decks, r.section)
                 for r in rows if r.section != "topcommanders"],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO tag_commanders (theme_slug, color_identity, commander_name, rank)"
                " VALUES (?,?,?,?)",
                [(theme, colors, name, i) for i, name in enumerate(parse_topcommanders(payload))],
            )
            _record(conn, url, status, len(rows))
            written += len(rows)

    conn.commit()
    return written
```

In `cli.py` add:

```python
def _cmd_fetch_labels(args: argparse.Namespace) -> int:
    from .labels.store import build_corpus, open_themes_db

    conn = open_themes_db(args.db)
    try:
        n = build_corpus(
            conn,
            commander_slugs=args.commanders.read_text().split(),
            theme_slugs=args.themes.split(","),
            color_identities=args.colors.split(","),
            cache_dir=args.cache_dir,
        )
    finally:
        conn.close()
    print(f"wrote {n} label rows -> {args.db}")
    return 0
```

and register it:

```python
    p = sub.add_parser("fetch-labels", help="Build the design-time EDHREC label corpus")
    p.add_argument("--commanders", type=Path, required=True, help="File of commander slugs, one per line")
    p.add_argument("--themes", default="sacrifice,reanimator,+1-1-counters")
    p.add_argument("--colors", default="jund,golgari,abzan,rakdos,jeskai")
    p.add_argument("--db", type=Path, default=Path("data/themes.db"))
    p.add_argument("--cache-dir", type=Path, default=Path(".cache/edhrec"))
    p.set_defaults(func=_cmd_fetch_labels)
```

Confirm the real theme slug for +1/+1 counters from a live `taglinks` response before hard-coding it — the default above is a guess and Task 8 depends on it being right.

- [ ] **Step 6: Run the suite and commit**

Run: `uv run pytest -v`
Expected: green.

```bash
git add -A
git commit -m "feat(labels): payload parsers, corpus builder, fetch-labels CLI"
```

---

### Task 8: EDHREC card-name resolution

**Files:**
- Create: `src/mtg_strategy_graph/labels/resolve.py`
- Test: `tests/labels/test_resolve.py`

**Interfaces:**
- Consumes: `ports.db` `cards` table, `themes.db` `theme_cards`.
- Produces: `resolve.build_name_map(ports_conn) -> dict[str, str]`; `resolve.resolve_names(names, name_map) -> tuple[dict[str, str], list[str]]` returning `(resolved, unresolved)`.

EDHREC keys by card name, the ports DB by the Forge card name. DFCs (`Fable of the Mirror-Breaker` vs `Fable of the Mirror-Breaker // Reflection of Kiki-Jiki`), split cards, and Adventure cards diverge. A silent 5% drop rate biases every mined signature, so unresolved names are counted and gated.

- [ ] **Step 1: Write the failing test**

`tests/labels/test_resolve.py`:

```python
from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.labels import resolve


def _ports_db(tmp_path, names):
    conn = open_db(tmp_path / "ports.db")
    conn.executemany("INSERT INTO cards (name) VALUES (?)", [(n,) for n in names])
    conn.commit()
    return conn


def test_exact_name_resolves(tmp_path):
    conn = _ports_db(tmp_path, ["Mayhem Devil"])
    try:
        resolved, unresolved = resolve.resolve_names(["Mayhem Devil"], resolve.build_name_map(conn))
        assert resolved == {"Mayhem Devil": "Mayhem Devil"}
        assert unresolved == []
    finally:
        conn.close()


def test_front_face_of_a_split_name_resolves(tmp_path):
    conn = _ports_db(tmp_path, ["Fable of the Mirror-Breaker"])
    try:
        resolved, unresolved = resolve.resolve_names(
            ["Fable of the Mirror-Breaker // Reflection of Kiki-Jiki"], resolve.build_name_map(conn)
        )
        assert resolved == {
            "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki": "Fable of the Mirror-Breaker"
        }
    finally:
        conn.close()


def test_case_and_punctuation_insensitive_fallback(tmp_path):
    conn = _ports_db(tmp_path, ["Ashnod's Altar"])
    try:
        resolved, _ = resolve.resolve_names(["Ashnod’s Altar"], resolve.build_name_map(conn))
        assert resolved == {"Ashnod’s Altar": "Ashnod's Altar"}
    finally:
        conn.close()


def test_unknown_name_is_reported_not_dropped(tmp_path):
    conn = _ports_db(tmp_path, ["Mayhem Devil"])
    try:
        resolved, unresolved = resolve.resolve_names(["Not A Real Card"], resolve.build_name_map(conn))
        assert resolved == {}
        assert unresolved == ["Not A Real Card"]
    finally:
        conn.close()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/labels/test_resolve.py -v`
Expected: FAIL — no module `mtg_strategy_graph.labels.resolve`.

- [ ] **Step 3: Write `resolve.py`**

```python
"""Map EDHREC card names onto the Forge card names in ports.db.

Resolution is deliberately conservative and ordered: exact, then front
face of a ``//`` name, then a normalised form that folds case, curly
apostrophes and punctuation. Anything left over is returned as
unresolved so the caller can gate on the drop rate rather than
discovering it as a quiet bias in a mined signature.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

_PUNCT = re.compile(r"[^a-z0-9]+")


def _normalise(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).replace("’", "'")
    return _PUNCT.sub("", folded.casefold())


def build_name_map(ports_conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{lookup_key: canonical_card_name}`` for every card."""
    out: dict[str, str] = {}
    for (name,) in ports_conn.execute("SELECT name FROM cards"):
        out[name] = name
        out.setdefault(_normalise(name), name)
    return out


def resolve_names(names, name_map: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for name in names:
        hit = name_map.get(name)
        if hit is None and "//" in name:
            hit = name_map.get(name.split("//")[0].strip())
            if hit is None:
                hit = name_map.get(_normalise(name.split("//")[0]))
        if hit is None:
            hit = name_map.get(_normalise(name))
        if hit is None:
            unresolved.append(name)
        else:
            resolved[name] = hit
    return resolved, unresolved
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/labels/test_resolve.py -v`
Expected: 4 passed.

- [ ] **Step 5: Add the corpus-wide coverage gate**

Append to `tests/labels/test_resolve.py`:

```python
import os
import sqlite3
from pathlib import Path

import pytest

MAX_UNRESOLVED_RATE = 0.02


@pytest.mark.integration
def test_corpus_name_resolution_coverage():
    ports = Path(os.environ.get("PARITY_NEW_DB", "data/ports.db"))
    themes = Path("data/themes.db")
    if not ports.exists() or not themes.exists():
        pytest.skip("ports.db or themes.db not built")
    pconn, tconn = open_db(ports, create=False), sqlite3.connect(themes)
    try:
        names = [r[0] for r in tconn.execute("SELECT DISTINCT card_name FROM theme_cards")]
        _, unresolved = resolve.resolve_names(names, resolve.build_name_map(pconn))
    finally:
        pconn.close()
        tconn.close()
    rate = len(unresolved) / max(len(names), 1)
    assert rate <= MAX_UNRESOLVED_RATE, (
        f"{len(unresolved)}/{len(names)} ({rate:.1%}) EDHREC names unresolved; "
        f"sample: {unresolved[:15]}"
    )
```

Run: `uv run pytest -m integration tests/labels/test_resolve.py -v`
Expected: pass. If it fails, read the sample and extend `_normalise` or add an alias branch — do not raise `MAX_UNRESOLVED_RATE`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(labels): EDHREC card-name resolution with a 2% unresolved gate"
```

---

### Task 9: Label metrics — inclusion, lift, core and discriminative sets

**Files:**
- Create: `src/mtg_strategy_graph/labels/metrics.py`
- Test: `tests/labels/test_metrics.py`

**Interfaces:**
- Consumes: `themes.db`.
- Produces: `metrics.inclusion_rates(conn, commander_slug, theme_slug) -> dict[str, float]`; `metrics.lift(target, others, *, alpha=0.01) -> dict[str, float]`; `metrics.core_label(rates, *, floor) -> set[str]`; `metrics.discriminative_label(conn, commander_slug, theme_slug, *, n, min_inclusion) -> list[str]`; `metrics.recall(top_n, labels) -> float`.

This is spec §6.1 in code. Discriminative recall is the gate; core recall is the sanity reading beside it.

- [ ] **Step 1: Write the failing test**

`tests/labels/test_metrics.py`:

```python
import pytest

from mtg_strategy_graph.labels import metrics
from mtg_strategy_graph.labels.store import open_themes_db

SAC = [("Mayhem Devil", 800, 1598), ("Sol Ring", 1500, 1598), ("Witch's Oven", 210, 1598)]
REA = [("Mayhem Devil", 20, 216), ("Sol Ring", 205, 216), ("Reanimate", 130, 216)]


@pytest.fixture
def conn(tmp_path):
    c = open_themes_db(tmp_path / "themes.db")
    rows = [("korvold", "sacrifice", n, None, nd, pd, "creatures") for n, nd, pd in SAC]
    rows += [("korvold", "reanimator", n, None, nd, pd, "creatures") for n, nd, pd in REA]
    c.executemany(
        "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
        " num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)", rows
    )
    c.commit()
    yield c
    c.close()


def test_inclusion_rates(conn):
    rates = metrics.inclusion_rates(conn, "korvold", "sacrifice")
    assert rates["Mayhem Devil"] == pytest.approx(800 / 1598)
    assert rates["Sol Ring"] == pytest.approx(1500 / 1598)


def test_zero_potential_decks_is_skipped_not_divided_by(conn):
    conn.execute(
        "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
        " num_decks, potential_decks, section) VALUES ('korvold','sacrifice','Broken',NULL,5,0,'creatures')"
    )
    assert "Broken" not in metrics.inclusion_rates(conn, "korvold", "sacrifice")


def test_lift_ranks_theme_distinctive_above_shared_staples(conn):
    sac = metrics.inclusion_rates(conn, "korvold", "sacrifice")
    rea = metrics.inclusion_rates(conn, "korvold", "reanimator")
    lifts = metrics.lift(sac, [rea])
    assert lifts["Witch's Oven"] > lifts["Mayhem Devil"] > lifts["Sol Ring"]


def test_discriminative_label_excludes_shared_staples(conn):
    label = metrics.discriminative_label(conn, "korvold", "sacrifice", n=2, min_inclusion=0.10)
    assert "Sol Ring" not in label
    assert "Witch's Oven" in label


def test_core_label_applies_the_floor(conn):
    rates = metrics.inclusion_rates(conn, "korvold", "sacrifice")
    assert metrics.core_label(rates, floor=0.5) == {"Mayhem Devil", "Sol Ring"}


def test_recall_is_intersection_over_label_size():
    assert metrics.recall(["a", "b", "c"], {"b", "c", "z"}) == pytest.approx(2 / 3)
    assert metrics.recall(["a"], set()) == 0.0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/labels/test_metrics.py -v`
Expected: FAIL — no module `mtg_strategy_graph.labels.metrics`.

- [ ] **Step 3: Write `metrics.py`**

```python
"""Label construction and recall metrics (spec §6.1).

The label is the theme page's per-card inclusion rate, not its High
Synergy Cards section. Measured 2026-08-15: Korvold's Sacrifice and
Reanimator high-synergy lists share 7 of 10, and the shared cards are
cEDH staples (Dark Ritual, Veil of Summer, Ragavan) with no thematic
content — gating on that would have capped the divergence metric at
the ground truth's own 30% separation. Ranking the same payload by
lift against the commander's other themes separates cleanly
(Jaccard of the top-15 distinctive sets: 0.000).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence


def inclusion_rates(conn: sqlite3.Connection, commander_slug: str, theme_slug: str) -> dict[str, float]:
    """Return ``{card_name: num_decks / potential_decks}`` for one pair.

    A card appearing in several sections is counted once, at its
    highest observed rate.
    """
    out: dict[str, float] = {}
    rows = conn.execute(
        "SELECT card_name, num_decks, potential_decks FROM theme_cards"
        " WHERE commander_slug = ? AND theme_slug = ?",
        (commander_slug, theme_slug),
    )
    for name, num, potential in rows:
        if not potential:
            continue
        rate = (num or 0) / potential
        if rate > out.get(name, -1.0):
            out[name] = rate
    return out


def lift(target: dict[str, float], others: Sequence[dict[str, float]], *, alpha: float = 0.01) -> dict[str, float]:
    """Smoothed ratio of a card's inclusion in ``target`` vs ``others``.

    ``alpha`` keeps a card absent from every comparison theme from
    producing an unbounded lift; without it the ranking is dominated by
    cards that merely fell below another page's section cutoff.
    """
    out: dict[str, float] = {}
    for name, rate in target.items():
        comparison = max((o.get(name, 0.0) for o in others), default=0.0)
        out[name] = (rate + alpha) / (comparison + alpha)
    return out


def core_label(rates: dict[str, float], *, floor: float) -> set[str]:
    """Cards played in at least ``floor`` of this theme's decks."""
    return {name for name, rate in rates.items() if rate >= floor}


def discriminative_label(
    conn: sqlite3.Connection,
    commander_slug: str,
    theme_slug: str,
    *,
    n: int,
    min_inclusion: float,
) -> list[str]:
    """The ``n`` cards most distinctive to this theme for this commander.

    Comparison set is the commander's *other* themes present in the
    corpus — the within-commander contrast of spec §5.1. Comparing
    across commanders instead would rank colour identity, not strategy.
    """
    target = inclusion_rates(conn, commander_slug, theme_slug)
    other_slugs = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT theme_slug FROM theme_cards WHERE commander_slug = ? AND theme_slug != ?",
            (commander_slug, theme_slug),
        )
    ]
    others = [inclusion_rates(conn, commander_slug, s) for s in other_slugs]
    lifts = lift(target, others)
    eligible = [name for name, rate in target.items() if rate >= min_inclusion]
    eligible.sort(key=lambda name: (-lifts[name], name))
    return eligible[:n]


def recall(ranked: Iterable[str], labels: set[str]) -> float:
    if not labels:
        return 0.0
    return len(set(ranked) & labels) / len(labels)
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/labels/test_metrics.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(labels): inclusion-rate labels, lift, core/discriminative sets"
```

---

### Task 10: Measure the baseline and pre-register the floors

**Files:**
- Create: `docs/baseline.json`, `docs/DECISIONS.md`
- Create (in the **old** repo): `scripts/measure_new_labels.py`
- Test: `tests/test_baseline_pinned.py`

**Interfaces:**
- Consumes: Task 9's metrics, the old repo's `SynergyEngine`.
- Produces: `docs/baseline.json` with `{"measured_at", "corpus", "core_floor", "discriminative_n", "min_inclusion", "per_pair": {...}, "aggregate": {...}, "gates": {"aggregate_discriminative_recall", "per_commander_minimum"}}`.

Spec §6.4 deliberately ships no numbers: the earlier ≥ 0.50 floor was calibrated against the High Synergy label that §6.1 discards. This task produces the numbers the rest of the slice is judged against, and it must run **before** any scoring work so the floor cannot be tuned to whatever the new engine happens to score.

- [ ] **Step 1: Build the corpus**

Pick the ~20 commanders per spec §8: Korvold, Reyhan, Yawgmoth and Karador are mandatory (the two failure modes plus the documented flood casualty); draw the rest from `tag_commanders` after the first fetch. Write the slugs to `data/slice_commanders.txt`, then:

```bash
uv run mtg-strategy-graph fetch-labels --commanders data/slice_commanders.txt
```

- [ ] **Step 2: Write the measurement script in the old repo**

`~/gofrolist/mtg-synergy-graph/scripts/measure_new_labels.py`:

```python
"""Measure the CURRENT engine against the new inclusion-rate labels.

Run from the mtg-synergy-graph repo; writes baseline.json for
mtg-strategy-graph to pin. This is the only task that touches the old
repo, and it only adds a script.

    uv run python scripts/measure_new_labels.py \
        --themes-db ~/gofrolist/mtg-strategy-graph/data/themes.db \
        --out ~/gofrolist/mtg-strategy-graph/docs/baseline.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "gofrolist/mtg-strategy-graph/src"))
from mtg_strategy_graph.labels import metrics  # noqa: E402

from mtg_synergy_graph import SynergyEngine  # noqa: E402
from mtg_synergy_graph.validate import commander_to_slug  # noqa: E402

CORE_FLOOR = 0.25
DISCRIMINATIVE_N = 20
MIN_INCLUSION = 0.10


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--themes-db", type=Path, required=True)
    ap.add_argument("--synergy-db", type=Path, default=Path("data/synergy.db"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    tconn = sqlite3.connect(args.themes_db)
    engine = SynergyEngine(db_path=str(args.synergy_db))
    pairs = tconn.execute(
        "SELECT DISTINCT commander_slug, theme_slug FROM theme_cards ORDER BY 1, 2"
    ).fetchall()

    slug_to_name = {}
    for name in {r[0] for r in tconn.execute("SELECT DISTINCT commander_name FROM tag_commanders")}:
        slug_to_name[commander_to_slug(name)] = name

    per_pair = {}
    for slug, theme in pairs:
        name = slug_to_name.get(slug)
        if name is None:
            continue
        try:
            page = engine.page(commander=name, offset=0, limit=args.top)
        except Exception as exc:  # commander missing from the ports DB
            per_pair[f"{slug}|{theme}"] = {"error": str(exc)}
            continue
        top = [i.card for i in page.items]
        core = metrics.core_label(metrics.inclusion_rates(tconn, slug, theme), floor=CORE_FLOOR)
        disc = set(metrics.discriminative_label(
            tconn, slug, theme, n=DISCRIMINATIVE_N, min_inclusion=MIN_INCLUSION))
        per_pair[f"{slug}|{theme}"] = {
            "core_recall": metrics.recall(top, core),
            "discriminative_recall": metrics.recall(top, disc),
            "core_label_size": len(core),
            "discriminative_label_size": len(disc),
            "pool_size": page.total,
        }

    scored = [v for v in per_pair.values() if "error" not in v]
    agg_disc = sum(v["discriminative_recall"] for v in scored) / max(len(scored), 1)
    agg_core = sum(v["core_recall"] for v in scored) / max(len(scored), 1)

    args.out.write_text(json.dumps({
        "measured_at": dt.datetime.now(dt.UTC).isoformat(),
        "engine": "mtg-synergy-graph (pre-rebuild baseline)",
        "core_floor": CORE_FLOOR,
        "discriminative_n": DISCRIMINATIVE_N,
        "min_inclusion": MIN_INCLUSION,
        "top_n": args.top,
        "per_pair": per_pair,
        "aggregate": {"core_recall": agg_core, "discriminative_recall": agg_disc},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"aggregate discriminative_recall={agg_disc:.3f} core_recall={agg_core:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run it**

```bash
cd ~/gofrolist/mtg-synergy-graph
uv run python scripts/measure_new_labels.py \
  --themes-db ~/gofrolist/mtg-strategy-graph/data/themes.db \
  --out ~/gofrolist/mtg-strategy-graph/docs/baseline.json
```

- [ ] **Step 4: Set the gates and record the reasoning**

Open `docs/baseline.json` and add a `"gates"` object by hand:

```json
"gates": {
  "aggregate_discriminative_recall": <baseline aggregate + 0.15>,
  "per_commander_minimum": <a value strictly above the worst observed pair, and never 0.0>
}
```

The shape is fixed by spec §6.4 — an aggregate floor plus a per-commander floor with **no zeros permitted**. Write the chosen numbers and one sentence of justification each into `docs/DECISIONS.md` under a `## 2026-08-15 — pre-registered gates` heading, together with the observed baseline. Committing the reasoning is what stops the floor drifting later to whatever the engine happens to produce.

- [ ] **Step 5: Pin the baseline with a test**

`tests/test_baseline_pinned.py`:

```python
import json
from pathlib import Path

BASELINE = Path("docs/baseline.json")


def test_baseline_exists_and_declares_gates():
    data = json.loads(BASELINE.read_text())
    gates = data["gates"]
    assert gates["per_commander_minimum"] > 0.0, "a zero floor defeats the purpose of the gate"
    assert gates["aggregate_discriminative_recall"] > data["aggregate"]["discriminative_recall"], (
        "the gate must require improvement over the measured baseline"
    )


def test_baseline_records_its_label_parameters():
    data = json.loads(BASELINE.read_text())
    assert {"core_floor", "discriminative_n", "min_inclusion", "top_n"} <= set(data)
```

Run: `uv run pytest tests/test_baseline_pinned.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit both repos**

```bash
cd ~/gofrolist/mtg-synergy-graph
git add scripts/measure_new_labels.py
git commit -m "feat(bench): measure the current engine against inclusion-rate labels"

cd ~/gofrolist/mtg-strategy-graph
git add -A
git commit -m "docs: pre-registered gates from the measured baseline"
```

---

### Task 11: Strategy catalog — schema, loader, validation

**Files:**
- Create: `src/mtg_strategy_graph/data/strategies.json`, `src/mtg_strategy_graph/strategy/__init__.py`, `src/mtg_strategy_graph/strategy/catalog.py`
- Test: `tests/strategy/test_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces: dataclasses `Rule(rule_id: str, weight_tier: str, candidate: dict)`, `Strategy(id: str, cls: str, edhrec_aliases: tuple[str, ...], availability: dict, manifest: tuple[str, ...])`, `Catalog(strategies: dict[str, Strategy], rules: dict[str, Rule], core: tuple[str, ...])`; `catalog.load_catalog(path: Path | None = None) -> Catalog`; `catalog.WEIGHT_TIERS: dict[str, float]`.

The catalog is the single source of truth for the partition. Validation is where deny-by-default stops being a slogan: a manifest naming a rule that does not exist, or a rule no manifest references, are both hard errors.

- [ ] **Step 1: Write the seed catalog `src/mtg_strategy_graph/data/strategies.json`**

```json
{
  "version": 1,
  "core": ["core_ramp", "core_draw"],
  "strategies": [
    {
      "id": "generic",
      "class": "tagged",
      "edhrec_aliases": [],
      "availability": {"always": true},
      "manifest": []
    },
    {
      "id": "reanimator",
      "class": "tagged",
      "edhrec_aliases": ["reanimator"],
      "availability": {
        "any_port": {
          "port_type": "effect",
          "event_class": "ChangeZone",
          "zone_origin_contains": "Graveyard",
          "zone_destination": "Battlefield"
        }
      },
      "manifest": ["reanimation_target"]
    }
  ],
  "rules": [
    {
      "rule_id": "core_ramp",
      "weight_tier": "support",
      "candidate": {"port_type": "effect", "event_class": "Mana"}
    },
    {
      "rule_id": "core_draw",
      "weight_tier": "support",
      "candidate": {"port_type": "effect", "event_class": "Draw"}
    },
    {
      "rule_id": "reanimation_target",
      "weight_tier": "primary",
      "candidate": {
        "port_type": "effect",
        "event_class": "ChangeZone",
        "zone_origin_contains": "Graveyard",
        "zone_destination": "Battlefield",
        "valid_filter_matches": "^Creature",
        "valid_filter_not_contains": "Opp"
      }
    }
  ]
}
```

This is a seed, not the finished catalog — Task 17 replaces the strategy list with the mined four plus the micro-strategy. `generic` carries an empty manifest on purpose: it scores on `core` alone, which is exactly spec §3.1's definition.

- [ ] **Step 2: Write the failing test**

`tests/strategy/test_catalog.py`:

```python
import json

import pytest

from mtg_strategy_graph.strategy import catalog


def _write(tmp_path, data):
    p = tmp_path / "strategies.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


BASE = {
    "version": 1,
    "core": ["core_ramp"],
    "strategies": [
        {"id": "generic", "class": "tagged", "edhrec_aliases": [], "availability": {"always": True},
         "manifest": []},
        {"id": "reanimator", "class": "tagged", "edhrec_aliases": ["reanimator"],
         "availability": {"any_port": {"port_type": "effect"}}, "manifest": ["reanimation_target"]},
    ],
    "rules": [
        {"rule_id": "core_ramp", "weight_tier": "support", "candidate": {"port_type": "effect"}},
        {"rule_id": "reanimation_target", "weight_tier": "primary", "candidate": {"port_type": "effect"}},
    ],
}


def test_loads_the_shipped_catalog():
    loaded = catalog.load_catalog()
    assert "generic" in loaded.strategies
    assert loaded.strategies["generic"].manifest == ()


def test_active_rules_are_manifest_plus_core(tmp_path):
    loaded = catalog.load_catalog(_write(tmp_path, BASE))
    assert set(catalog.active_rule_ids(loaded, "generic")) == set(loaded.core)
    assert set(catalog.active_rule_ids(loaded, "reanimator")) == {"core_ramp", "reanimation_target"}


def test_manifest_naming_an_unknown_rule_is_rejected(tmp_path):
    bad = json.loads(json.dumps(BASE))
    bad["strategies"][1]["manifest"] = ["does_not_exist"]
    with pytest.raises(ValueError, match="unknown rule"):
        catalog.load_catalog(_write(tmp_path, bad))


def test_orphan_rule_is_rejected(tmp_path):
    bad = json.loads(json.dumps(BASE))
    bad["rules"].append({"rule_id": "orphan", "weight_tier": "primary", "candidate": {"port_type": "effect"}})
    with pytest.raises(ValueError, match="orphan"):
        catalog.load_catalog(_write(tmp_path, bad))


def test_unknown_weight_tier_is_rejected(tmp_path):
    bad = json.loads(json.dumps(BASE))
    bad["rules"][0]["weight_tier"] = "enormous"
    with pytest.raises(ValueError, match="weight_tier"):
        catalog.load_catalog(_write(tmp_path, bad))


def test_duplicate_rule_id_is_rejected(tmp_path):
    bad = json.loads(json.dumps(BASE))
    bad["rules"].append(dict(bad["rules"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        catalog.load_catalog(_write(tmp_path, bad))
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/strategy/test_catalog.py -v`
Expected: FAIL — no module `mtg_strategy_graph.strategy`.

- [ ] **Step 4: Write `catalog.py`**

```python
"""The strategy catalog: strategies, their rule manifests, and the rules.

Deny-by-default lives here. ``active_rule_ids`` is the only way to learn
which rules a query may use, and everything downstream takes that list
as its universe — a rule outside it is never loaded, never scored, and
never enters an IDF denominator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

#: Multiplier applied to a rule's IDF weight. A closed set: an unknown
#: tier is a hard error, so a typo cannot silently score as 1.0.
WEIGHT_TIERS: dict[str, float] = {"primary": 1.0, "secondary": 0.6, "support": 0.3}

STRATEGY_CLASSES: frozenset[str] = frozenset({"tagged", "micro"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    weight_tier: str
    candidate: dict


@dataclass(frozen=True)
class Strategy:
    id: str
    cls: str
    edhrec_aliases: tuple[str, ...]
    availability: dict
    manifest: tuple[str, ...]


@dataclass(frozen=True)
class Catalog:
    strategies: dict[str, Strategy]
    rules: dict[str, Rule]
    core: tuple[str, ...]


def _default_path() -> Path:
    return Path(str(resources.files("mtg_strategy_graph.data").joinpath("strategies.json")))


def load_catalog(path: Path | None = None) -> Catalog:
    """Load and validate the catalog. Raises ``ValueError`` on any defect."""
    raw = json.loads((path or _default_path()).read_text(encoding="utf-8"))

    rules: dict[str, Rule] = {}
    for entry in raw["rules"]:
        rule_id = entry["rule_id"]
        if rule_id in rules:
            raise ValueError(f"duplicate rule_id: {rule_id}")
        tier = entry["weight_tier"]
        if tier not in WEIGHT_TIERS:
            raise ValueError(f"unknown weight_tier {tier!r} on rule {rule_id}; expected {sorted(WEIGHT_TIERS)}")
        rules[rule_id] = Rule(rule_id, tier, entry["candidate"])

    core = tuple(raw.get("core", ()))
    strategies: dict[str, Strategy] = {}
    referenced: set[str] = set(core)
    for entry in raw["strategies"]:
        cls = entry.get("class", "tagged")
        if cls not in STRATEGY_CLASSES:
            raise ValueError(f"unknown strategy class {cls!r}; expected {sorted(STRATEGY_CLASSES)}")
        manifest = tuple(entry.get("manifest", ()))
        for rule_id in manifest:
            if rule_id not in rules:
                raise ValueError(f"strategy {entry['id']} manifest names unknown rule {rule_id!r}")
        referenced.update(manifest)
        strategies[entry["id"]] = Strategy(
            id=entry["id"],
            cls=cls,
            edhrec_aliases=tuple(entry.get("edhrec_aliases", ())),
            availability=entry["availability"],
            manifest=manifest,
        )

    for rule_id in core:
        if rule_id not in rules:
            raise ValueError(f"core names unknown rule {rule_id!r}")

    orphans = sorted(set(rules) - referenced)
    if orphans:
        raise ValueError(f"orphan rules referenced by no manifest and not in core: {orphans}")

    if "generic" not in strategies:
        raise ValueError("catalog must define a 'generic' strategy (spec §3.1)")

    return Catalog(strategies=strategies, rules=rules, core=core)


def active_rule_ids(catalog: Catalog, strategy_id: str) -> tuple[str, ...]:
    """The complete rule universe for a query. Deny-by-default: nothing else exists."""
    if strategy_id not in catalog.strategies:
        raise ValueError(f"unknown strategy {strategy_id!r}; known: {sorted(catalog.strategies)}")
    manifest = catalog.strategies[strategy_id].manifest
    seen: dict[str, None] = {}
    for rule_id in (*catalog.core, *manifest):
        seen[rule_id] = None
    return tuple(seen)
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/strategy/test_catalog.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(strategy): catalog schema, loader, and deny-by-default validation"
```

---

### Task 12: The predicate DSL

**Files:**
- Create: `src/mtg_strategy_graph/strategy/predicates.py`
- Test: `tests/strategy/test_predicates.py`

**Interfaces:**
- Consumes: `card_ports` and `port_attributes` from `ports.db`.
- Produces: `predicates.compile_predicate(spec: dict) -> CompiledPredicate` with `.sql: str`, `.params: tuple`, `.post_filter: Callable[[sqlite3.Row], bool]`; `predicates.matching_cards(conn, spec) -> set[str]`; `predicates.PORT_COLUMNS: frozenset[str]`; `predicates.OPS: frozenset[str]`.

Every rule and every availability gate is one of these. The op set is closed and the column allowlist is explicit — an unrecognised key is a `ValueError`, never a silently ignored clause, because a silently dropped clause widens a rule's pool without anyone noticing.

- [ ] **Step 1: Write the failing test**

`tests/strategy/test_predicates.py`:

```python
import pytest

from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.strategy import predicates

PORTS = [
    # (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination)
    ("Reanimate", "effect", "ChangeZone", "Creature", "Graveyard", "Battlefield"),
    ("Animate Dead", "effect", "ChangeZone", "Enchanted", "Graveyard", "Battlefield"),
    ("Bone Miser", "effect", "ChangeZone", "Creature.OppCtrl", "Graveyard", "Battlefield"),
    ("Crop Rotation", "effect", "ChangeZone", "Land.YouOwn", "Graveyard", "Battlefield"),
    ("Sol Ring", "effect", "Mana", None, None, None),
]


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "ports.db")
    c.executemany("INSERT INTO cards (name) VALUES (?)", [(p[0],) for p in PORTS])
    c.executemany(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter,"
        " zone_origin, zone_destination) VALUES (?,?,?,?,?,?)", PORTS
    )
    c.commit()
    yield c
    c.close()


def test_equality_clauses(conn):
    got = predicates.matching_cards(conn, {"port_type": "effect", "event_class": "Mana"})
    assert got == {"Sol Ring"}


def test_contains_clause(conn):
    got = predicates.matching_cards(
        conn, {"event_class": "ChangeZone", "zone_origin_contains": "Graveyard"}
    )
    assert got == {"Reanimate", "Animate Dead", "Bone Miser", "Crop Rotation"}


def test_regex_and_negation_narrow_the_pool(conn):
    got = predicates.matching_cards(conn, {
        "port_type": "effect",
        "event_class": "ChangeZone",
        "zone_origin_contains": "Graveyard",
        "zone_destination": "Battlefield",
        "valid_filter_matches": "^Creature",
        "valid_filter_not_contains": "Opp",
    })
    assert got == {"Reanimate"}, "Land and opponent-scoped reanimation must be excluded"


def test_null_valid_filter_does_not_match_a_regex_clause(conn):
    got = predicates.matching_cards(conn, {"port_type": "effect", "valid_filter_matches": "^Creature"})
    assert "Sol Ring" not in got


def test_unknown_key_is_rejected(conn):
    with pytest.raises(ValueError, match="unknown predicate key"):
        predicates.matching_cards(conn, {"port_typo": "effect"})


def test_unknown_column_in_op_is_rejected(conn):
    with pytest.raises(ValueError, match="unknown predicate key"):
        predicates.matching_cards(conn, {"nonsense_contains": "x"})


def test_empty_predicate_is_rejected(conn):
    with pytest.raises(ValueError, match="empty predicate"):
        predicates.matching_cards(conn, {})


def test_attribute_clause_joins_port_attributes(conn):
    port_id = conn.execute("SELECT id FROM card_ports WHERE card_name='Reanimate'").fetchone()[0]
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?,?,?)",
        (port_id, "type", "Creature"),
    )
    conn.commit()
    got = predicates.matching_cards(conn, {"attribute": {"kind": "type", "value": "Creature"}})
    assert got == {"Reanimate"}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/strategy/test_predicates.py -v`
Expected: FAIL — no module `mtg_strategy_graph.strategy.predicates`.

- [ ] **Step 3: Write `predicates.py`**

```python
"""The rule predicate DSL: a JSON clause set compiled to SQL plus a Python filter.

Design constraints:

* The column allowlist and op set are closed frozensets. An unknown key
  raises ``ValueError`` rather than being ignored — a silently dropped
  clause widens a rule's candidate pool, which is exactly the flood
  pathology the partition exists to prevent.
* Column names are only ever interpolated after an allowlist check;
  values are always bound parameters. Never ``assert`` for this —
  ``python -O`` strips asserts.
* Regex ops run in Python because SQLite has no portable REGEXP.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

PORT_COLUMNS: frozenset[str] = frozenset({
    "port_type", "event_class", "valid_filter", "zone_origin", "zone_destination",
    "counter_type", "granted_keyword", "replacement_result", "cost_subtype", "branch_kind",
})

OPS: frozenset[str] = frozenset({"eq", "contains", "not_contains", "matches"})

_SUFFIXES = {"_contains": "contains", "_not_contains": "not_contains", "_matches": "matches"}


@dataclass(frozen=True)
class CompiledPredicate:
    sql: str
    params: tuple
    post_filter: Callable[[sqlite3.Row], bool]


def _split_key(key: str) -> tuple[str, str]:
    """Return ``(column, op)``. Longest suffix wins so ``_not_contains`` beats ``_contains``."""
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if key.endswith(suffix):
            column = key[: -len(suffix)]
            if column in PORT_COLUMNS:
                return column, _SUFFIXES[suffix]
    if key in PORT_COLUMNS:
        return key, "eq"
    raise ValueError(f"unknown predicate key: {key!r}")


def compile_predicate(spec: dict) -> CompiledPredicate:
    if not spec:
        raise ValueError("empty predicate: a rule must constrain at least one column")

    where: list[str] = ["1=1"]
    params: list = []
    regexes: list[tuple[str, re.Pattern]] = []
    joins = ""

    for key, value in spec.items():
        if key == "attribute":
            kind, val = value["kind"], value["value"]
            joins = " JOIN port_attributes pa ON pa.port_id = p.id"
            where.append("pa.attr_kind = ? AND pa.attr_value = ? AND pa.is_negated = 0")
            params.extend([kind, val])
            continue

        column, op = _split_key(key)
        if op == "eq":
            where.append(f"p.{column} = ?")
            params.append(value)
        elif op == "contains":
            where.append(f"instr(COALESCE(p.{column}, ''), ?) > 0")
            params.append(value)
        elif op == "not_contains":
            where.append(f"instr(COALESCE(p.{column}, ''), ?) = 0")
            params.append(value)
        elif op == "matches":
            where.append(f"p.{column} IS NOT NULL")
            regexes.append((column, re.compile(value)))

    def post_filter(row: sqlite3.Row) -> bool:
        return all(pattern.search(row[column] or "") for column, pattern in regexes)

    sql = (
        "SELECT DISTINCT p.card_name, "
        + ", ".join(f"p.{c}" for c in sorted(PORT_COLUMNS))
        + f" FROM card_ports p{joins} WHERE "
        + " AND ".join(where)
    )
    return CompiledPredicate(sql=sql, params=tuple(params), post_filter=post_filter)


def matching_cards(conn: sqlite3.Connection, spec: dict) -> set[str]:
    """Every card with at least one port satisfying ``spec``."""
    compiled = compile_predicate(spec)
    return {
        row["card_name"]
        for row in conn.execute(compiled.sql, compiled.params)
        if compiled.post_filter(row)
    }
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/strategy/test_predicates.py -v`
Expected: 8 passed.

- [ ] **Step 5: Verify the DSL against the live DB**

```bash
uv run python -c "
from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.strategy import predicates
conn = open_db('data/ports.db', create=False)
broad = {'port_type':'effect','event_class':'ChangeZone','zone_origin_contains':'Graveyard','zone_destination':'Battlefield'}
tight = dict(broad, valid_filter_matches='^Creature', valid_filter_not_contains='Opp')
print('broad', len(predicates.matching_cards(conn, broad)))
print('tight', len(predicates.matching_cards(conn, tight)))
"
```

Expected: `broad 693`, `tight 245` — the numbers recorded in spec §3.3. A mismatch means the DSL is not reproducing the reference SQL and must be fixed before any rule is authored on top of it.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(strategy): closed-vocabulary predicate DSL over card_ports"
```

---

### Task 13: The deny-by-default scorer with strategy-local IDF

**Files:**
- Create: `src/mtg_strategy_graph/strategy/scoring.py`
- Test: `tests/strategy/test_scoring.py`

**Interfaces:**
- Consumes: `catalog.load_catalog`, `catalog.active_rule_ids`, `predicates.matching_cards`, `ports.db`.
- Produces: `scoring.ScoredCard(name: str, score: float, rules: tuple[str, ...], contributions: dict[str, float])`; `scoring.available_strategies(conn, catalog, commander) -> tuple[str, ...]`; `scoring.score_commander(conn, catalog, commander, strategy, *, top_n=30, deck=()) -> list[ScoredCard]`; `scoring.pool_size(conn, catalog, commander, strategy) -> int`.

Spec §3.2 in code. Two properties carry the whole design: only `active_rule_ids` rules are consulted, and the IDF denominator is the strategy-local pool.

- [ ] **Step 1: Confirm the columns the scorer needs**

```bash
uv run python -c "
from mtg_strategy_graph.db import open_db
conn = open_db('data/ports.db', create=False)
print([d[1] for d in conn.execute('PRAGMA table_info(cards)')])
"
```

Expected to include `name`, `color_identity`, `cmc`, `legal_commander`, `types`. If `legal_commander` is absent the moved importer does not populate it; in that case drop the legality clause from the SQL below and open an issue — do not invent the column.

- [ ] **Step 2: Write the failing test**

`tests/strategy/test_scoring.py`:

```python
import json

import pytest

from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.strategy import catalog as catalog_mod
from mtg_strategy_graph.strategy import scoring

CATALOG = {
    "version": 1,
    "core": ["core_ramp"],
    "strategies": [
        {"id": "generic", "class": "tagged", "edhrec_aliases": [], "availability": {"always": True},
         "manifest": []},
        {"id": "reanimator", "class": "tagged", "edhrec_aliases": ["reanimator"],
         "availability": {"any_port": {"event_class": "Sacrifice"}},
         "manifest": ["reanimation_target"]},
        {"id": "tokens", "class": "tagged", "edhrec_aliases": ["tokens"],
         "availability": {"any_port": {"event_class": "Sacrifice"}},
         "manifest": ["token_maker"]},
    ],
    "rules": [
        {"rule_id": "core_ramp", "weight_tier": "support", "candidate": {"event_class": "Mana"}},
        {"rule_id": "reanimation_target", "weight_tier": "primary",
         "candidate": {"event_class": "ChangeZone", "zone_origin_contains": "Graveyard"}},
        {"rule_id": "token_maker", "weight_tier": "primary", "candidate": {"event_class": "Token"}},
    ],
}

CARDS = [
    # (name, color_identity, cmc, legal_commander)
    ("Cmdr", "B,G,R", 5, 1),
    ("Reanimate", "B", 1, 1),
    ("Animate Dead", "B", 2, 1),
    ("Sol Ring", "", 1, 1),
    ("Bitterblossom", "B", 2, 1),
    ("Blue Card", "U", 2, 1),
]
PORTS = [
    ("Cmdr", "effect", "Sacrifice", None, None, None),
    ("Reanimate", "effect", "ChangeZone", "Creature", "Graveyard", "Battlefield"),
    ("Animate Dead", "effect", "ChangeZone", "Enchanted", "Graveyard", "Battlefield"),
    ("Sol Ring", "effect", "Mana", None, None, None),
    ("Bitterblossom", "effect", "Token", None, None, None),
    ("Blue Card", "effect", "ChangeZone", "Creature", "Graveyard", "Battlefield"),
]


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "ports.db")
    c.executemany(
        "INSERT INTO cards (name, color_identity, cmc, legal_commander) VALUES (?,?,?,?)", CARDS
    )
    c.executemany(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter,"
        " zone_origin, zone_destination) VALUES (?,?,?,?,?,?)", PORTS
    )
    c.commit()
    yield c
    c.close()


@pytest.fixture
def cat(tmp_path):
    p = tmp_path / "strategies.json"
    p.write_text(json.dumps(CATALOG), encoding="utf-8")
    return catalog_mod.load_catalog(p)


def test_only_manifest_rules_reach_the_output(conn, cat):
    got = scoring.score_commander(conn, cat, "Cmdr", "reanimator")
    names = {c.name for c in got}
    assert "Bitterblossom" not in names, "a tokens-only rule must not fire for reanimator"
    assert {"Reanimate", "Animate Dead"} <= names


def test_core_rules_fire_for_every_strategy(conn, cat):
    for strategy in ("generic", "reanimator", "tokens"):
        names = {c.name for c in scoring.score_commander(conn, cat, "Cmdr", strategy)}
        assert "Sol Ring" in names


def test_generic_scores_on_core_alone(conn, cat):
    names = {c.name for c in scoring.score_commander(conn, cat, "Cmdr", "generic")}
    assert names == {"Sol Ring"}


def test_colour_identity_is_enforced(conn, cat):
    names = {c.name for c in scoring.score_commander(conn, cat, "Cmdr", "reanimator")}
    assert "Blue Card" not in names


def test_commander_is_not_recommended_to_itself(conn, cat):
    assert "Cmdr" not in {c.name for c in scoring.score_commander(conn, cat, "Cmdr", "reanimator")}


def test_idf_denominator_is_the_strategy_pool_not_the_card_table(conn, cat):
    """A rule matching every card in its pool must contribute less than a selective one."""
    got = {c.name: c for c in scoring.score_commander(conn, cat, "Cmdr", "reanimator")}
    assert got["Reanimate"].contributions["reanimation_target"] > 0
    assert got["Sol Ring"].contributions["core_ramp"] > 0


def test_sort_key_has_no_popularity_term(conn, cat):
    """Ties break on (cmc, name) only — no edhrec_rank anywhere in the engine."""
    import inspect

    source = inspect.getsource(scoring)
    assert "edhrec" not in source.lower()


def test_available_strategies_filters_on_the_availability_gate(conn, cat):
    assert set(scoring.available_strategies(conn, cat, "Cmdr")) == {"generic", "reanimator", "tokens"}
    conn.execute("INSERT INTO cards (name, color_identity, cmc, legal_commander) VALUES ('Plain','B',2,1)")
    conn.commit()
    assert set(scoring.available_strategies(conn, cat, "Plain")) == {"generic"}


def test_unknown_strategy_raises(conn, cat):
    with pytest.raises(ValueError, match="unknown strategy"):
        scoring.score_commander(conn, cat, "Cmdr", "not_a_strategy")
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/strategy/test_scoring.py -v`
Expected: FAIL — no module `mtg_strategy_graph.strategy.scoring`.

- [ ] **Step 4: Write `scoring.py`**

```python
"""Deny-by-default scoring with strategy-local IDF (spec §3.2).

    score(card | commander, strategy) =    Σ         tier_r · idf_r(card)
                                    r ∈ manifest ∪ core

Two properties do the work. **Deny-by-default**: only rules from
``active_rule_ids`` are loaded, so a rule belonging to another strategy
cannot contribute a single float. **Strategy-local IDF**: the
denominator is the strategy's own candidate pool, so a card that is
unremarkable among aristocrats payoffs can be extraordinary in a
counters pool — a distinction global IDF cannot express.

There is deliberately no popularity term anywhere: no EDHREC rank in
the score, and none in the sort key. Ties break on ``(cmc, name)``.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .catalog import WEIGHT_TIERS, Catalog, active_rule_ids
from .predicates import matching_cards


@dataclass(frozen=True)
class ScoredCard:
    name: str
    score: float
    rules: tuple[str, ...]
    contributions: dict[str, float]


def _commander_row(conn: sqlite3.Connection, commander: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT name, color_identity FROM cards WHERE name = ?", (commander,)
    ).fetchone()
    if row is None:
        raise ValueError(f"commander not found in ports.db: {commander!r}")
    return row


def _colour_pips(value: str | None) -> frozenset[str]:
    return frozenset(p for p in (value or "").split(",") if p)


def _legal_candidates(conn: sqlite3.Connection, commander: str) -> dict[str, tuple[float, str]]:
    """``{card_name: (cmc, name)}`` for every legal in-colour candidate."""
    identity = _colour_pips(_commander_row(conn, commander)["color_identity"])
    out: dict[str, tuple[float, str]] = {}
    for row in conn.execute(
        "SELECT name, color_identity, cmc FROM cards WHERE COALESCE(legal_commander, 1) = 1"
    ):
        if row["name"] == commander:
            continue
        if not _colour_pips(row["color_identity"]) <= identity:
            continue
        out[row["name"]] = (float(row["cmc"] or 0), row["name"])
    return out


def _availability_holds(conn: sqlite3.Connection, commander: str, availability: dict) -> bool:
    if availability.get("always"):
        return True
    spec = availability.get("any_port")
    if spec is None:
        return False
    return commander in matching_cards(conn, spec)


def available_strategies(conn: sqlite3.Connection, catalog: Catalog, commander: str) -> tuple[str, ...]:
    """Strategies this commander can mechanically play, in catalog order."""
    return tuple(
        s.id for s in catalog.strategies.values()
        if _availability_holds(conn, commander, s.availability)
    )


def _rule_matches(
    conn: sqlite3.Connection, catalog: Catalog, strategy: str, candidates: dict
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for rule_id in active_rule_ids(catalog, strategy):
        matched = matching_cards(conn, catalog.rules[rule_id].candidate)
        out[rule_id] = matched & candidates.keys()
    return out


def pool_size(conn: sqlite3.Connection, catalog: Catalog, commander: str, strategy: str) -> int:
    """How many cards any active rule reaches — the coverage diagnostic of spec §6.3."""
    candidates = _legal_candidates(conn, commander)
    matches = _rule_matches(conn, catalog, strategy, candidates)
    return len(set().union(*matches.values()) if matches else set())


def score_commander(
    conn: sqlite3.Connection,
    catalog: Catalog,
    commander: str,
    strategy: str,
    *,
    top_n: int = 30,
    deck: tuple[str, ...] = (),
) -> list[ScoredCard]:
    """Rank candidates for ``(commander, strategy)``.

    ``deck`` is accepted and ignored: the interface is fixed now so
    that adding deck-context conditioning later (spec sub-project F) is
    not a breaking change.
    """
    del deck

    candidates = _legal_candidates(conn, commander)
    matches = _rule_matches(conn, catalog, strategy, candidates)
    pool = set().union(*matches.values()) if matches else set()
    if not pool:
        return []

    idf: dict[str, float] = {}
    for rule_id, matched in matches.items():
        if not matched:
            continue
        idf[rule_id] = WEIGHT_TIERS[catalog.rules[rule_id].weight_tier] * math.log2(
            1.0 + len(pool) / len(matched)
        )

    scored: list[ScoredCard] = []
    for name in pool:
        contributions = {r: idf[r] for r, matched in matches.items() if r in idf and name in matched}
        if not contributions:
            continue
        scored.append(
            ScoredCard(
                name=name,
                score=sum(contributions.values()),
                rules=tuple(sorted(contributions)),
                contributions=contributions,
            )
        )

    scored.sort(key=lambda c: (-c.score, candidates[c.name][0], c.name))
    return scored[:top_n]
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/strategy/test_scoring.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(strategy): deny-by-default scorer with strategy-local IDF"
```

---

### Task 14: The isolation invariant and the no-EDHREC-at-inference guard

**Files:**
- Test: `tests/strategy/test_isolation.py`, `tests/strategy/test_no_edhrec_at_inference.py`

**Interfaces:**
- Consumes: everything from Tasks 11–13. Adds no production code.

This is the task the whole rebuild exists for. Spec §3.4: *the ranked output for strategy S is a function of only the rules in `manifest(S) ∪ core`*. The test mutates every rule outside S — adds synthetic rules, perturbs weights, deletes rules — and asserts S's output is bitwise identical.

- [ ] **Step 1: Write the isolation test**

`tests/strategy/test_isolation.py`:

```python
"""The invariant that makes single-strategy work possible.

The four DECLINE cycles in the predecessor project failed because
adding a rule perturbed the IDF denominators of every commander sharing
any rule with it (spec §1.1, channel (b)). Under the partition that is
structurally impossible, and this test is the proof — it is expected to
be the first test that breaks if anyone reintroduces a global
normalisation term.
"""

from __future__ import annotations

import copy
import json

import pytest

from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.strategy import catalog as catalog_mod
from mtg_strategy_graph.strategy import scoring

BASE = {
    "version": 1,
    "core": ["core_ramp"],
    "strategies": [
        {"id": "generic", "class": "tagged", "edhrec_aliases": [], "availability": {"always": True},
         "manifest": []},
        {"id": "alpha", "class": "tagged", "edhrec_aliases": [], "availability": {"always": True},
         "manifest": ["rule_a1", "rule_a2"]},
        {"id": "beta", "class": "tagged", "edhrec_aliases": [], "availability": {"always": True},
         "manifest": ["rule_b1"]},
    ],
    "rules": [
        {"rule_id": "core_ramp", "weight_tier": "support", "candidate": {"event_class": "Mana"}},
        {"rule_id": "rule_a1", "weight_tier": "primary",
         "candidate": {"event_class": "ChangeZone", "zone_origin_contains": "Graveyard"}},
        {"rule_id": "rule_a2", "weight_tier": "secondary", "candidate": {"event_class": "Draw"}},
        {"rule_id": "rule_b1", "weight_tier": "primary", "candidate": {"event_class": "Token"}},
    ],
}

CARDS = [("Cmdr", "B,G,R", 5), ("Reanimate", "B", 1), ("Sol Ring", "", 1),
         ("Bitterblossom", "B", 2), ("Night's Whisper", "B", 2), ("Skullclamp", "", 1)]
PORTS = [
    ("Cmdr", "effect", "Sacrifice", None, None),
    ("Reanimate", "effect", "ChangeZone", "Graveyard", "Battlefield"),
    ("Sol Ring", "effect", "Mana", None, None),
    ("Bitterblossom", "effect", "Token", None, None),
    ("Night's Whisper", "effect", "Draw", None, None),
    ("Skullclamp", "effect", "Draw", None, None),
]


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "ports.db")
    c.executemany("INSERT INTO cards (name, color_identity, cmc) VALUES (?,?,?)", CARDS)
    c.executemany(
        "INSERT INTO card_ports (card_name, port_type, event_class, zone_origin, zone_destination)"
        " VALUES (?,?,?,?,?)", PORTS
    )
    c.commit()
    yield c
    c.close()


def _load(tmp_path, data, name):
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return catalog_mod.load_catalog(p)


def _fingerprint(conn, cat, strategy):
    """Exact output: names, order, and scores to full float precision."""
    return [
        (c.name, c.score.hex(), c.rules)
        for c in scoring.score_commander(conn, cat, "Cmdr", strategy, top_n=100)
    ]


MUTATIONS = {
    "add_rule_to_other_strategy": lambda d: (
        d["rules"].append({"rule_id": "rule_b2", "weight_tier": "primary",
                           "candidate": {"event_class": "Draw"}}),
        d["strategies"][2]["manifest"].append("rule_b2"),
    ),
    "change_other_rule_weight": lambda d: d["rules"].__setitem__(
        3, {**d["rules"][3], "weight_tier": "support"}
    ),
    "broaden_other_rule_predicate": lambda d: d["rules"].__setitem__(
        3, {**d["rules"][3], "candidate": {"port_type": "effect"}}
    ),
    "delete_other_strategy": lambda d: (
        d["strategies"].pop(2), d["rules"].pop(3),
    ),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_alpha_is_bitwise_identical_under_mutations_to_beta(conn, tmp_path, mutation):
    baseline = _fingerprint(conn, _load(tmp_path, copy.deepcopy(BASE), "base.json"), "alpha")

    mutated = copy.deepcopy(BASE)
    MUTATIONS[mutation](mutated)
    after = _fingerprint(conn, _load(tmp_path, mutated, f"{mutation}.json"), "alpha")

    assert after == baseline, f"mutation {mutation!r} leaked into strategy 'alpha'"


def test_the_test_can_actually_detect_a_leak(conn, tmp_path):
    """Guard against a vacuous invariant: mutating alpha's OWN rules must change alpha."""
    baseline = _fingerprint(conn, _load(tmp_path, copy.deepcopy(BASE), "b2.json"), "alpha")
    mutated = copy.deepcopy(BASE)
    mutated["rules"][1] = {**mutated["rules"][1], "weight_tier": "support"}
    after = _fingerprint(conn, _load(tmp_path, mutated, "m2.json"), "alpha")
    assert after != baseline
```

`test_the_test_can_actually_detect_a_leak` is not optional. Without it, an invariant test that always passes because the fingerprint is constant would look like success.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/strategy/test_isolation.py -v`
Expected: 5 passed. A failure here means a global term crept into scoring — fix `scoring.py`, never the test.

- [ ] **Step 3: Write the no-EDHREC-at-inference guard**

`tests/strategy/test_no_edhrec_at_inference.py`:

```python
"""themes.db must be unreachable from the recommendation path (spec §4.2).

Structural, not conventional: the inference modules must not import the
label or mining packages, and must contain no EDHREC identifiers.
"""

from __future__ import annotations

import ast
from pathlib import Path

INFERENCE_MODULES = [
    Path("src/mtg_strategy_graph/strategy/scoring.py"),
    Path("src/mtg_strategy_graph/strategy/catalog.py"),
    Path("src/mtg_strategy_graph/strategy/predicates.py"),
    Path("src/mtg_strategy_graph/db.py"),
]

FORBIDDEN_PREFIXES = ("mtg_strategy_graph.labels", "mtg_strategy_graph.mining")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            if node.level:
                found.add(f"relative:{node.module}")
    return found


def test_inference_modules_do_not_import_labels_or_mining():
    for path in INFERENCE_MODULES:
        imported = _imported_modules(path)
        leaks = [m for m in imported for p in FORBIDDEN_PREFIXES if m.startswith(p)]
        assert not leaks, f"{path} imports design-time-only modules: {leaks}"
        assert "labels" not in imported and "mining" not in imported


def test_inference_modules_mention_no_edhrec_identifiers():
    for path in INFERENCE_MODULES:
        text = path.read_text(encoding="utf-8").lower()
        code = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("#")
        )
        assert "edhrec" not in code, f"{path} references EDHREC in executable code"
        assert "themes.db" not in code, f"{path} references themes.db"
```

The docstring-stripping is deliberate: `scoring.py`'s module docstring legitimately explains that no EDHREC term exists, and a naive substring scan would flag that sentence. The filter above removes `#` comments; if a docstring mention trips the test, move the explanation to a `#` comment rather than weakening the assertion.

- [ ] **Step 4: Run it**

Run: `uv run pytest tests/strategy/test_no_edhrec_at_inference.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test(strategy): isolation invariant + no-EDHREC-at-inference guard

The isolation test mutates every rule outside a strategy and asserts
bitwise-identical output, with a leak-detection control so the
invariant cannot pass vacuously."
```

---

### Task 15: The signature miner

**Files:**
- Create: `src/mtg_strategy_graph/mining/__init__.py`, `src/mtg_strategy_graph/mining/miner.py`
- Modify: `src/mtg_strategy_graph/cli.py` (add `mine`)
- Test: `tests/mining/test_miner.py`

**Interfaces:**
- Consumes: `themes.db`, `ports.db`, `labels.resolve`, `labels.metrics`.
- Produces: `miner.card_patterns(conn, card_name) -> set[Pattern]` where `Pattern = tuple[tuple[str, str], ...]` (a sorted, hashable form of a predicate dict); `miner.pattern_to_predicate(pattern) -> dict`; `miner.mine(ports_conn, themes_conn, theme_slug, *, min_cards, min_commanders, alpha) -> list[Proposal]`; `Proposal(pattern, predicate, log_odds, n_positive, n_background, n_commanders, examples)`.

Spec §5. Patterns are emitted in the exact shape the predicate DSL consumes, so promoting a proposal into `strategies.json` is a copy, not a translation.

- [ ] **Step 1: Write the failing test**

`tests/mining/test_miner.py`:

```python
import pytest

from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.labels.store import open_themes_db
from mtg_strategy_graph.mining import miner

PORTS = [
    ("Reanimate", "effect", "ChangeZone", "Creature", "Graveyard", "Battlefield", None),
    ("Animate Dead", "effect", "ChangeZone", "Enchanted", "Graveyard", "Battlefield", None),
    ("Exhume", "effect", "ChangeZone", "Creature", "Graveyard", "Battlefield", None),
    ("Bitterblossom", "effect", "Token", None, None, None, None),
    ("Ophiomancer", "effect", "Token", None, None, None, None),
    ("Sol Ring", "effect", "Mana", None, None, None, None),
]


@pytest.fixture
def ports(tmp_path):
    c = open_db(tmp_path / "ports.db")
    c.executemany("INSERT INTO cards (name) VALUES (?)", [(p[0],) for p in PORTS])
    c.executemany(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter,"
        " zone_origin, zone_destination, counter_type) VALUES (?,?,?,?,?,?,?)", PORTS
    )
    c.commit()
    yield c
    c.close()


@pytest.fixture
def themes(tmp_path):
    c = open_themes_db(tmp_path / "themes.db")
    rows = []
    for cmdr in ("meren", "karador", "chainer"):
        for name in ("Reanimate", "Animate Dead", "Exhume", "Sol Ring"):
            rows.append((cmdr, "reanimator", name, None, 50, 100, "creatures"))
        for name in ("Bitterblossom", "Ophiomancer", "Sol Ring"):
            rows.append((cmdr, "tokens", name, None, 50, 100, "creatures"))
    c.executemany(
        "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
        " num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)", rows
    )
    c.commit()
    yield c
    c.close()


def test_card_patterns_emit_multiple_granularities(ports):
    patterns = miner.card_patterns(ports, "Reanimate")
    predicates = [miner.pattern_to_predicate(p) for p in patterns]
    assert {"port_type": "effect", "event_class": "ChangeZone"} in predicates
    assert {
        "port_type": "effect", "event_class": "ChangeZone",
        "zone_origin": "Graveyard", "zone_destination": "Battlefield",
    } in predicates


def test_patterns_are_hashable_and_round_trip(ports):
    for pattern in miner.card_patterns(ports, "Reanimate"):
        assert hash(pattern) is not None
        assert miner.pattern_to_predicate(pattern)


def test_mining_ranks_the_defining_shape_first(ports, themes):
    proposals = miner.mine(ports, themes, "reanimator", min_cards=2, min_commanders=2)
    top = miner.pattern_to_predicate(proposals[0].pattern)
    assert top["event_class"] == "ChangeZone"
    assert top.get("zone_destination") in (None, "Battlefield")


def test_shared_staples_do_not_promote(ports, themes):
    """Sol Ring is on both theme lists, so its Mana pattern must not rank."""
    proposals = miner.mine(ports, themes, "reanimator", min_cards=1, min_commanders=2)
    ranked = [miner.pattern_to_predicate(p.pattern) for p in proposals]
    manas = [p for p in ranked if p.get("event_class") == "Mana"]
    changezones = [p for p in ranked if p.get("event_class") == "ChangeZone"]
    assert changezones, "the defining pattern must appear"
    if manas:
        assert ranked.index(manas[0]) > ranked.index(changezones[0])


def test_support_floors_are_enforced(ports, themes):
    strict = miner.mine(ports, themes, "reanimator", min_cards=99, min_commanders=2)
    assert strict == []


def test_proposal_records_provenance(ports, themes):
    p = miner.mine(ports, themes, "reanimator", min_cards=2, min_commanders=2)[0]
    assert p.n_commanders >= 2
    assert p.n_positive >= 2
    assert p.examples
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/mining/test_miner.py -v`
Expected: FAIL — no module `mtg_strategy_graph.mining`.

- [ ] **Step 3: Write `miner.py`**

```python
"""Mine port-pattern signatures from the EDHREC label corpus (spec §5).

Design-time only. Emits proposals for human review and never mutates
``strategies.json``.

The contrast is **within-commander**: positives are cards on the (C, T)
lists of commanders tagged T; the background is those *same* commanders'
other theme lists. Contrasting across different commanders instead
learns colour and commander bias — "Reanimator means black" — a
useless-but-plausible signal that would pass a naive eval.

Aggregation across commanders is also what makes the result trustworthy
despite tag noise. Measured 2026-08-15: Korvold's ``Reanimator`` cohort
is lands-matter, not reanimation. A per-commander label is not evidence
about what a strategy means; only the aggregate is.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

Pattern = tuple[tuple[str, str], ...]

#: Columns a mined pattern may constrain, coarsest first. Kept a subset
#: of ``predicates.PORT_COLUMNS`` so every proposal is directly
#: promotable into a rule row.
_GRANULARITIES: Sequence[tuple[str, ...]] = (
    ("port_type", "event_class"),
    ("port_type", "event_class", "zone_origin", "zone_destination"),
    ("port_type", "event_class", "counter_type"),
    ("port_type", "event_class", "granted_keyword"),
)


@dataclass(frozen=True)
class Proposal:
    pattern: Pattern
    predicate: dict
    log_odds: float
    n_positive: int
    n_background: int
    n_commanders: int
    examples: tuple[str, ...]


def pattern_to_predicate(pattern: Pattern) -> dict:
    return dict(pattern)


def card_patterns(ports_conn: sqlite3.Connection, card_name: str) -> set[Pattern]:
    """Every pattern this card's ports exhibit, across all granularities."""
    columns = sorted({c for g in _GRANULARITIES for c in g})
    rows = ports_conn.execute(
        f"SELECT {', '.join(columns)} FROM card_ports WHERE card_name = ?", (card_name,)
    ).fetchall()

    out: set[Pattern] = set()
    for row in rows:
        for granularity in _GRANULARITIES:
            items = [(c, row[c]) for c in granularity]
            if any(v is None or v == "" for _, v in items):
                continue
            out.add(tuple(sorted((c, str(v)) for c, v in items)))
    return out


def _theme_cards(themes_conn, commander: str, theme: str) -> set[str]:
    return {
        r[0]
        for r in themes_conn.execute(
            "SELECT DISTINCT card_name FROM theme_cards WHERE commander_slug=? AND theme_slug=?",
            (commander, theme),
        )
    }


def mine(
    ports_conn: sqlite3.Connection,
    themes_conn: sqlite3.Connection,
    theme_slug: str,
    *,
    min_cards: int = 20,
    min_commanders: int = 5,
    alpha: float = 0.5,
    limit: int = 60,
) -> list[Proposal]:
    commanders = [
        r[0]
        for r in themes_conn.execute(
            "SELECT DISTINCT commander_slug FROM theme_cards WHERE theme_slug = ?", (theme_slug,)
        )
    ]

    positives: set[str] = set()
    background: set[str] = set()
    commanders_by_card: dict[str, set[str]] = {}
    for commander in commanders:
        target = _theme_cards(themes_conn, commander, theme_slug)
        positives |= target
        for card in target:
            commanders_by_card.setdefault(card, set()).add(commander)
        others = {
            r[0]
            for r in themes_conn.execute(
                "SELECT DISTINCT theme_slug FROM theme_cards WHERE commander_slug=? AND theme_slug!=?",
                (commander, theme_slug),
            )
        }
        for other in others:
            background |= _theme_cards(themes_conn, commander, other)

    background -= positives

    pattern_positive: dict[Pattern, set[str]] = {}
    pattern_background: dict[Pattern, set[str]] = {}
    for card in positives:
        for pattern in card_patterns(ports_conn, card):
            pattern_positive.setdefault(pattern, set()).add(card)
    for card in background:
        for pattern in card_patterns(ports_conn, card):
            pattern_background.setdefault(pattern, set()).add(card)

    n_pos, n_bg = max(len(positives), 1), max(len(background), 1)
    proposals: list[Proposal] = []
    for pattern, cards in pattern_positive.items():
        supporting = set().union(*(commanders_by_card.get(c, set()) for c in cards)) if cards else set()
        if len(cards) < min_cards or len(supporting) < min_commanders:
            continue
        bg = pattern_background.get(pattern, set())
        log_odds = math.log2(((len(cards) + alpha) / n_pos) / ((len(bg) + alpha) / n_bg))
        proposals.append(
            Proposal(
                pattern=pattern,
                predicate=pattern_to_predicate(pattern),
                log_odds=log_odds,
                n_positive=len(cards),
                n_background=len(bg),
                n_commanders=len(supporting),
                examples=tuple(sorted(cards)[:8]),
            )
        )

    proposals.sort(key=lambda p: (-p.log_odds, -p.n_positive, sorted(p.pattern)))
    return proposals[:limit]
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/mining/test_miner.py -v`
Expected: 6 passed.

- [ ] **Step 5: Add the `mine` subcommand**

In `cli.py`:

```python
def _cmd_mine(args: argparse.Namespace) -> int:
    import json as _json

    from .db import open_db as _open_db
    from .labels.store import open_themes_db
    from .mining.miner import mine

    ports, themes = _open_db(args.ports_db, create=False), open_themes_db(args.themes_db, create=False)
    try:
        proposals = mine(
            ports, themes, args.theme,
            min_cards=args.min_cards, min_commanders=args.min_commanders,
        )
    finally:
        ports.close()
        themes.close()

    args.out.write_text(_json.dumps([
        {
            "predicate": p.predicate, "log_odds": p.log_odds, "n_positive": p.n_positive,
            "n_background": p.n_background, "n_commanders": p.n_commanders,
            "examples": list(p.examples),
        }
        for p in proposals
    ], indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(proposals)} proposals -> {args.out}")
    return 0
```

and register:

```python
    p = sub.add_parser("mine", help="Mine port-pattern signature proposals for a theme")
    p.add_argument("--theme", required=True)
    p.add_argument("--ports-db", type=Path, default=Path("data/ports.db"))
    p.add_argument("--themes-db", type=Path, default=Path("data/themes.db"))
    p.add_argument("--min-cards", type=int, default=20)
    p.add_argument("--min-commanders", type=int, default=5)
    p.add_argument("--out", type=Path, default=Path("signature_proposal.json"))
    p.set_defaults(func=_cmd_mine)
```

- [ ] **Step 6: Run the miner against the real corpus and check kill criterion 1**

```bash
uv run mtg-strategy-graph mine --theme reanimator --out proposals_reanimator.json
head -40 proposals_reanimator.json
```

Expected: a `ChangeZone` / `Graveyard` → `Battlefield` pattern in the top few. Per spec §9 criterion 1 this must be judged **on the cross-commander aggregate**, never on Korvold alone — Korvold's Reanimator cohort is lands-matter and a correct signature would appear to fail there. If the aggregate does not surface it, stop and record the failure in `docs/DECISIONS.md` before continuing.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(mining): within-commander contrast signature miner + mine CLI"
```

---

### Task 16: The anti-whitelist generalisation guard

**Files:**
- Create: `src/mtg_strategy_graph/mining/guard.py`
- Test: `tests/mining/test_guard.py`

**Interfaces:**
- Consumes: `predicates.matching_cards`, `themes.db`.
- Produces: `guard.generalisation_ratio(ports_conn, themes_conn, predicate) -> float`; `guard.holdout_support(ports_conn, themes_conn, predicate, theme_slug, *, seed=17, train_fraction=0.8) -> tuple[int, int]`; `guard.GENERALISATION_FLOOR: float`.

Spec §5.4. Without this a mined "signature" can be a memorised EDHREC list wearing a predicate costume — and guard 2 is also what preserves hidden-gem discovery, since a card on no EDHREC list still matches when its ports do.

- [ ] **Step 1: Write the failing test**

`tests/mining/test_guard.py`:

```python
import pytest

from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.labels.store import open_themes_db
from mtg_strategy_graph.mining import guard

PORTS = [
    ("Reanimate", "effect", "ChangeZone"),
    ("Exhume", "effect", "ChangeZone"),
    ("Obscure Card A", "effect", "ChangeZone"),
    ("Obscure Card B", "effect", "ChangeZone"),
    ("Sol Ring", "effect", "Mana"),
]


@pytest.fixture
def ports(tmp_path):
    c = open_db(tmp_path / "ports.db")
    c.executemany("INSERT INTO cards (name) VALUES (?)", [(p[0],) for p in PORTS])
    c.executemany(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?,?,?)", PORTS
    )
    c.commit()
    yield c
    c.close()


@pytest.fixture
def themes(tmp_path):
    c = open_themes_db(tmp_path / "themes.db")
    c.executemany(
        "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
        " num_decks, potential_decks, section) VALUES (?,?,?,?,?,?,?)",
        [("meren", "reanimator", n, None, 50, 100, "creatures") for n in ("Reanimate", "Exhume")],
    )
    c.commit()
    yield c
    c.close()


def test_generalising_predicate_scores_high(ports, themes):
    ratio = guard.generalisation_ratio(ports, themes, {"event_class": "ChangeZone"})
    assert ratio == pytest.approx(0.5), "2 of 4 matched cards are on no training list"
    assert ratio >= guard.GENERALISATION_FLOOR


def test_predicate_matching_only_training_cards_is_rejected(ports, themes):
    ports.execute("INSERT INTO cards (name) VALUES ('Narrow')")
    ports.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Narrow','effect','Narrow')"
    )
    themes.execute(
        "INSERT INTO theme_cards (commander_slug, theme_slug, card_name, synergy,"
        " num_decks, potential_decks, section) VALUES ('meren','reanimator','Narrow',NULL,50,100,'x')"
    )
    ports.commit()
    themes.commit()
    ratio = guard.generalisation_ratio(ports, themes, {"event_class": "Narrow"})
    assert ratio == 0.0
    assert ratio < guard.GENERALISATION_FLOOR


def test_predicate_matching_only_unlabelled_cards_scores_one(ports, themes):
    """Sol Ring is on no theme list here, so every match generalises."""
    assert guard.generalisation_ratio(ports, themes, {"event_class": "Mana"}) == 1.0


def test_predicate_matching_nothing_scores_zero(ports, themes):
    assert guard.generalisation_ratio(ports, themes, {"event_class": "Nonexistent"}) == 0.0


def test_holdout_support_is_deterministic(ports, themes):
    first = guard.holdout_support(ports, themes, {"event_class": "ChangeZone"}, "reanimator")
    second = guard.holdout_support(ports, themes, {"event_class": "ChangeZone"}, "reanimator")
    assert first == second
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/mining/test_guard.py -v`
Expected: FAIL — no module `mtg_strategy_graph.mining.guard`.

- [ ] **Step 3: Write `guard.py`**

```python
"""Anti-whitelist guards for mined signatures (spec §5.4).

A pattern must be *expressible* (it is, by construction — it compiles
to a port predicate), must *generalise*, and must *hold out
commanders*. The generalisation ratio is the load-bearing one: a
pattern that matches only cards already on a training list has
memorised EDHREC rather than described a mechanic. It is also what
keeps hidden-gem discovery alive — a card on no list still matches when
its ports do.
"""

from __future__ import annotations

import random
import sqlite3

from ..strategy.predicates import matching_cards

#: Minimum fraction of a predicate's matches that must lie outside every
#: training list. Calibrated in Task 17 against the real corpus; a
#: pattern below this is a whitelist in costume.
GENERALISATION_FLOOR: float = 0.25


def _all_labelled_cards(themes_conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in themes_conn.execute("SELECT DISTINCT card_name FROM theme_cards")}


def generalisation_ratio(
    ports_conn: sqlite3.Connection, themes_conn: sqlite3.Connection, predicate: dict
) -> float:
    """Fraction of matched cards that appear on no training list."""
    matched = matching_cards(ports_conn, predicate)
    if not matched:
        return 0.0
    return len(matched - _all_labelled_cards(themes_conn)) / len(matched)


def holdout_support(
    ports_conn: sqlite3.Connection,
    themes_conn: sqlite3.Connection,
    predicate: dict,
    theme_slug: str,
    *,
    seed: int = 17,
    train_fraction: float = 0.8,
) -> tuple[int, int]:
    """Return ``(held_out_cards_matched, held_out_cards_total)``.

    Commanders are split deterministically, so a proposal's support on
    unseen commanders is reproducible across runs.
    """
    commanders = sorted(
        r[0]
        for r in themes_conn.execute(
            "SELECT DISTINCT commander_slug FROM theme_cards WHERE theme_slug = ?", (theme_slug,)
        )
    )
    rng = random.Random(seed)
    shuffled = list(commanders)
    rng.shuffle(shuffled)
    held_out = shuffled[int(len(shuffled) * train_fraction) :]
    if not held_out:
        return (0, 0)

    placeholders = ",".join("?" * len(held_out))
    cards = {
        r[0]
        for r in themes_conn.execute(
            "SELECT DISTINCT card_name FROM theme_cards"
            f" WHERE theme_slug = ? AND commander_slug IN ({placeholders})",
            (theme_slug, *held_out),
        )
    }
    matched = matching_cards(ports_conn, predicate)
    return (len(cards & matched), len(cards))
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/mining/test_guard.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(mining): generalisation ratio + commander holdout guards"
```

---

### Task 17: Author the four strategies and one micro-strategy

**Files:**
- Modify: `src/mtg_strategy_graph/data/strategies.json`
- Create: `tests/strategy/test_shipped_catalog.py`
- Modify: `docs/DECISIONS.md`

**Interfaces:**
- Consumes: proposals from Task 15, guards from Task 16, the DSL from Task 12.
- Produces: a catalog with strategies `generic`, `aristocrats`, `reanimator`, `plus1_counters`, plus one `micro` strategy.

This task is human judgment applied to machine proposals. Nothing is promoted automatically.

- [ ] **Step 1: Mine all three tagged strategies**

```bash
for t in reanimator aristocrats sacrifice; do
  uv run mtg-strategy-graph mine --theme "$t" --out "proposals_$t.json"
done
```

Use the real slug for +1/+1 counters as discovered in Task 7 Step 5.

- [ ] **Step 2: Classify the tag vocabulary (spec §4.3)**

Before authoring anything, list the tags present in the corpus and sort each into one of three buckets, recording the decision in `docs/DECISIONS.md`:

```bash
uv run python -c "
import sqlite3
c = sqlite3.connect('data/themes.db')
for slug, label, n in c.execute(
    'SELECT theme_slug, label, SUM(deck_count) FROM commander_themes'
    ' GROUP BY theme_slug ORDER BY 3 DESC LIMIT 60'
):
    print(f'{n or 0:>7}  {slug:<28} {label}')
"
```

- **distinct strategies** — get their own catalog entry (Reanimator, Tokens, +1/+1 Counters, Landfall…)
- **aliases to merge** — Sacrifice and Aristocrats nearly nest; they become **one** strategy carrying both slugs in `edhrec_aliases`, which is also why the design dropped multi-select (spec D5)
- **not strategies** — deck attributes rather than spines: Budget, Midrange, Good Stuff, Combo, cEDH. These get no catalog entry.

- [ ] **Step 3: Promote proposals into rule rows**

For each strategy, take proposals top-down and for each one:

1. Read `predicate`, `n_positive`, `n_commanders`, `examples`.
2. Check it against the guard:

```bash
uv run python -c "
from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.labels.store import open_themes_db
from mtg_strategy_graph.mining import guard
p, t = open_db('data/ports.db', create=False), open_themes_db('data/themes.db', create=False)
pred = {'port_type':'effect','event_class':'ChangeZone','zone_origin':'Graveyard','zone_destination':'Battlefield'}
print('generalisation', guard.generalisation_ratio(p, t, pred))
print('holdout', guard.holdout_support(p, t, pred, 'reanimator'))
"
```

3. Reject anything below `GENERALISATION_FLOOR` or with zero held-out support.
4. Check the matched-card count with `predicates.matching_cards`. A rule matching more than ~800 cards is a flood — narrow it with a `valid_filter_matches` or `*_not_contains` clause, exactly as spec §3.3's worked example narrows 693 to 245.
5. Add the surviving predicate as a rule row with a `weight_tier`, and list its `rule_id` in the strategy's manifest.

Aim for 3–6 rules per strategy. Record every rejection and its reason in `docs/DECISIONS.md` — the rejections are the most reusable output of this task.

- [ ] **Step 4: Author the micro-strategy**

Pick a commander with a rare mechanic and no useful EDHREC tag. Find candidates:

```bash
uv run python -c "
from mtg_strategy_graph.db import open_db
conn = open_db('data/ports.db', create=False)
sql = '''SELECT p.event_class, COUNT(DISTINCT p.card_name) n
         FROM card_ports p JOIN cards c ON c.name = p.card_name
         WHERE c.types LIKE '%Legendary%' AND c.types LIKE '%Creature%'
         GROUP BY p.event_class HAVING n BETWEEN 2 AND 8 ORDER BY n'''
for row in conn.execute(sql): print(dict(row))
"
```

Write it as a `class: "micro"` strategy with a narrow `availability` gate and a 1–3 rule manifest. It must satisfy spec §3.1's four constraints: a port predicate rather than a card list, never auto-applied, an availability predicate whose commander count you can state, and no aggregate gate.

- [ ] **Step 5: Write the shipped-catalog test**

`tests/strategy/test_shipped_catalog.py`:

```python
import pytest

from mtg_strategy_graph.db import open_db
from mtg_strategy_graph.strategy import catalog as catalog_mod
from mtg_strategy_graph.strategy import predicates, scoring

EXPECTED = {"generic", "aristocrats", "reanimator", "plus1_counters"}
FLOOD_CEILING = 800


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load_catalog()


def test_shipped_catalog_defines_the_slice_strategies(cat):
    assert EXPECTED <= set(cat.strategies)


def test_exactly_one_micro_strategy_ships(cat):
    micro = [s for s in cat.strategies.values() if s.cls == "micro"]
    assert len(micro) == 1, "the slice ships one worked micro-strategy example (spec §8)"
    assert 1 <= len(micro[0].manifest) <= 3


def test_generic_has_an_empty_manifest(cat):
    assert cat.strategies["generic"].manifest == ()


def test_every_rule_predicate_compiles(cat):
    for rule in cat.rules.values():
        predicates.compile_predicate(rule.candidate)


@pytest.mark.integration
def test_no_rule_floods_the_pool(cat):
    conn = open_db("data/ports.db", create=False)
    try:
        oversized = {
            rule_id: len(predicates.matching_cards(conn, rule.candidate))
            for rule_id, rule in cat.rules.items()
        }
    finally:
        conn.close()
    flooding = {k: v for k, v in oversized.items() if v > FLOOD_CEILING}
    assert not flooding, f"rules matching more than {FLOOD_CEILING} cards will flood: {flooding}"


@pytest.mark.integration
def test_micro_strategy_covers_few_commanders(cat):
    conn = open_db("data/ports.db", create=False)
    try:
        micro = next(s for s in cat.strategies.values() if s.cls == "micro")
        spec = micro.availability["any_port"]
        legends = {
            r[0] for r in conn.execute(
                "SELECT name FROM cards WHERE types LIKE '%Legendary%' AND types LIKE '%Creature%'"
            )
        }
        n = len(predicates.matching_cards(conn, spec) & legends)
    finally:
        conn.close()
    assert 1 <= n <= 5, f"micro-strategy claims {n} commanders; over 5 it is misfiled (spec §3.1)"
```

- [ ] **Step 6: Run everything**

Run: `uv run pytest -v && uv run pytest -m integration -v`
Expected: green. A `test_no_rule_floods_the_pool` failure means a rule needs narrowing, not a higher ceiling.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(strategy): author aristocrats, reanimator, plus1_counters + one micro-strategy"
```

---

### Task 18: The `recommend` command with `--explain`

**Files:**
- Modify: `src/mtg_strategy_graph/cli.py`
- Modify: `src/mtg_strategy_graph/__init__.py`
- Test: `tests/test_cli_recommend.py`

**Interfaces:**
- Consumes: `scoring.score_commander`, `scoring.available_strategies`, `catalog.load_catalog`.
- Produces: `mtg_strategy_graph.recommend(commander, strategy, *, db, top_n=30, deck=()) -> list[ScoredCard]`; CLI `recommend --commander X [--strategy Y] [--explain] [--top N]`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli_recommend.py`:

```python
import pytest

from mtg_strategy_graph import cli


@pytest.mark.integration
def test_recommend_prints_a_ranked_list(capsys):
    rc = cli.main(["recommend", "--commander", "Korvold, Fae-Cursed King",
                   "--strategy", "aristocrats", "--top", "5", "--db", "data/ports.db"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "aristocrats" in out
    assert len([ln for ln in out.splitlines() if ln.strip().startswith(("1", "2", "3"))]) >= 3


@pytest.mark.integration
def test_explain_names_the_firing_rules(capsys):
    cli.main(["recommend", "--commander", "Korvold, Fae-Cursed King", "--strategy", "aristocrats",
              "--top", "3", "--explain", "--db", "data/ports.db"])
    out = capsys.readouterr().out
    assert "rule:" in out


@pytest.mark.integration
def test_unavailable_strategy_lists_the_available_ones(capsys):
    rc = cli.main(["recommend", "--commander", "Korvold, Fae-Cursed King",
                   "--strategy", "plus1_counters", "--db", "data/ports.db"])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert rc in (0, 3)
    if rc == 3:
        assert "available:" in out


@pytest.mark.integration
def test_default_strategy_is_generic(capsys):
    cli.main(["recommend", "--commander", "Korvold, Fae-Cursed King", "--top", "3",
              "--db", "data/ports.db"])
    assert "generic" in capsys.readouterr().out
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest -m integration tests/test_cli_recommend.py -v`
Expected: FAIL — `argparse` rejects the unknown `recommend` subcommand.

- [ ] **Step 3: Add `recommend` to `cli.py`**

```python
def _cmd_recommend(args: argparse.Namespace) -> int:
    from .db import open_db as _open_db
    from .strategy.catalog import load_catalog
    from .strategy.scoring import available_strategies, pool_size, score_commander

    catalog = load_catalog()
    conn = _open_db(args.db, create=False)
    try:
        available = available_strategies(conn, catalog, args.commander)
        if args.strategy not in available:
            print(
                f"strategy {args.strategy!r} is not available for {args.commander}; "
                f"available: {', '.join(available)}",
                file=sys.stderr,
            )
            return 3
        results = score_commander(conn, catalog, args.commander, args.strategy, top_n=args.top)
        pool = pool_size(conn, catalog, args.commander, args.strategy)
    finally:
        conn.close()

    print(f"commander: {args.commander}")
    print(f"strategy:  {args.strategy}   (pool {pool} cards)")
    print(f"{'rank':>4}  {'card':<38} {'score':>7}")
    for rank, card in enumerate(results, start=1):
        print(f"{rank:>4}  {card.name:<38} {card.score:>7.3f}")
        if args.explain:
            for rule_id in card.rules:
                print(f"        rule: {rule_id:<28} +{card.contributions[rule_id]:.3f}")
    return 0
```

Register it:

```python
    p = sub.add_parser("recommend", help="Rank cards for a commander under a strategy")
    p.add_argument("--commander", required=True)
    p.add_argument("--strategy", default="generic")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--explain", action="store_true")
    p.add_argument("--db", type=Path, default=Path("data/ports.db"))
    p.set_defaults(func=_cmd_recommend)
```

And expose the library entry point in `__init__.py`:

```python
from pathlib import Path

from .db import open_db
from .strategy.catalog import load_catalog
from .strategy.scoring import ScoredCard, score_commander

__all__ = ["ScoredCard", "__version__", "recommend"]


def recommend(
    commander: str,
    strategy: str = "generic",
    *,
    db: str | Path = "data/ports.db",
    top_n: int = 30,
    deck: tuple[str, ...] = (),
) -> list[ScoredCard]:
    """Rank cards for ``(commander, strategy)``.

    ``deck`` is accepted and ignored today; the parameter exists so that
    deck-context conditioning can land later without a breaking change.
    """
    conn = open_db(db, create=False)
    try:
        return score_commander(
            conn, load_catalog(), commander, strategy, top_n=top_n, deck=deck
        )
    finally:
        conn.close()
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest -m integration tests/test_cli_recommend.py -v`
Expected: 4 passed.

- [ ] **Step 5: Look at the output**

```bash
uv run mtg-strategy-graph recommend --commander "Korvold, Fae-Cursed King" --strategy aristocrats --top 30 --explain
uv run mtg-strategy-graph recommend --commander "Korvold, Fae-Cursed King" --strategy reanimator --top 30
uv run mtg-strategy-graph recommend --commander "Yawgmoth, Thran Physician" --strategy aristocrats --top 30
```

The Yawgmoth run is the one that matters: his pool under the old engine was **49 cards** and all ten of his labels were outside it. Note the reported pool size — if the strategy-supplied pool has not expanded it, kill criterion 4 is in play.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(cli): recommend command with --explain and library entry point"
```

---

### Task 19: Evaluation report and golden snapshots

**Files:**
- Create: `src/mtg_strategy_graph/eval/__init__.py`, `src/mtg_strategy_graph/eval/report.py`
- Modify: `src/mtg_strategy_graph/cli.py` (add `evaluate`)
- Test: `tests/eval/test_report.py`, `tests/eval/test_golden.py`

**Interfaces:**
- Consumes: `labels.metrics`, `strategy.scoring`, `docs/baseline.json`.
- Produces: `report.evaluate(ports_conn, themes_conn, catalog, pairs, *, top_n, params) -> EvalResult`; `EvalResult.per_pair: dict`, `.aggregate: dict`, `.to_markdown() -> str`; `report.divergence(results_by_strategy) -> float`.

Every metric in spec §6.4 lands here: discriminative recall (the gate), core recall, novelty rate, cross-strategy divergence, and pool size.

- [ ] **Step 1: Write the failing test**

`tests/eval/test_report.py`:

```python
import pytest

from mtg_strategy_graph.eval import report


def test_divergence_is_zero_for_identical_lists():
    assert report.divergence({"a": ["x", "y"], "b": ["x", "y"]}) == pytest.approx(0.0)


def test_divergence_is_one_for_disjoint_lists():
    assert report.divergence({"a": ["x"], "b": ["y"]}) == pytest.approx(1.0)


def test_divergence_of_a_single_strategy_is_undefined_as_zero():
    assert report.divergence({"a": ["x"]}) == 0.0


def test_novelty_rate_counts_cards_on_no_label_list():
    assert report.novelty_rate(["a", "b", "c"], {"a"}) == pytest.approx(2 / 3)


def test_novelty_rate_of_an_empty_ranking_is_zero():
    assert report.novelty_rate([], {"a"}) == 0.0


def test_markdown_renders_one_row_per_pair():
    result = report.EvalResult(
        per_pair={
            "korvold|aristocrats": {"discriminative_recall": 0.4, "core_recall": 0.5,
                                    "novelty_rate": 0.3, "pool_size": 812},
        },
        aggregate={"discriminative_recall": 0.4, "core_recall": 0.5, "novelty_rate": 0.3,
                   "divergence": 0.8},
        params={"top_n": 30},
    )
    md = result.to_markdown()
    assert "korvold|aristocrats" in md
    assert "0.400" in md
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/eval/test_report.py -v`
Expected: FAIL — no module `mtg_strategy_graph.eval`.

- [ ] **Step 3: Write `report.py`**

```python
"""Evaluation report: the metrics of spec §6.4.

Discriminative recall is the gate. Core recall sits beside it as a
sanity reading. Novelty rate is the guard that stops the gate being
satisfied by becoming an EDHREC mirror, and cross-strategy divergence
is the EDHREC-free measure of whether choosing a strategy changed
anything at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from ..labels import metrics
from ..strategy.catalog import Catalog
from ..strategy.scoring import pool_size, score_commander


def divergence(results_by_strategy: dict[str, Sequence[str]]) -> float:
    """Mean pairwise Jaccard *distance* between strategies' rankings.

    1.0 means the strategies share nothing; 0.0 means picking a
    strategy changed nothing, which is project failure (spec §9).
    """
    keys = sorted(results_by_strategy)
    if len(keys) < 2:
        return 0.0
    distances = []
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            sa, sb = set(results_by_strategy[a]), set(results_by_strategy[b])
            union = sa | sb
            distances.append(1.0 - (len(sa & sb) / len(union) if union else 0.0))
    return sum(distances) / len(distances)


def novelty_rate(ranked: Sequence[str], labelled: set[str]) -> float:
    """Fraction of the ranking that appears on no EDHREC list."""
    if not ranked:
        return 0.0
    return len([c for c in ranked if c not in labelled]) / len(ranked)


@dataclass(frozen=True)
class EvalResult:
    per_pair: dict
    aggregate: dict
    params: dict

    def to_markdown(self) -> str:
        lines = [
            f"# Evaluation (top_n={self.params.get('top_n')})",
            "",
            "| pair | discriminative | core | novelty | pool |",
            "|---|---:|---:|---:|---:|",
        ]
        for key in sorted(self.per_pair):
            row = self.per_pair[key]
            lines.append(
                f"| {key} | {row['discriminative_recall']:.3f} | {row['core_recall']:.3f} "
                f"| {row['novelty_rate']:.3f} | {row['pool_size']} |"
            )
        lines += ["", "## Aggregate", ""]
        for key in sorted(self.aggregate):
            lines.append(f"- **{key}**: {self.aggregate[key]:.3f}")
        return "\n".join(lines) + "\n"


def evaluate(
    ports_conn: sqlite3.Connection,
    themes_conn: sqlite3.Connection,
    catalog: Catalog,
    pairs: Sequence[tuple[str, str, str]],
    *,
    top_n: int,
    core_floor: float,
    discriminative_n: int,
    min_inclusion: float,
) -> EvalResult:
    """``pairs`` is ``(commander_name, commander_slug, strategy_id)``."""
    labelled = {r[0] for r in themes_conn.execute("SELECT DISTINCT card_name FROM theme_cards")}
    per_pair: dict[str, dict] = {}
    by_commander: dict[str, dict[str, list[str]]] = {}

    for name, slug, strategy in pairs:
        ranked = [c.name for c in score_commander(ports_conn, catalog, name, strategy, top_n=top_n)]
        rates = metrics.inclusion_rates(themes_conn, slug, strategy)
        core = metrics.core_label(rates, floor=core_floor)
        disc = set(
            metrics.discriminative_label(
                themes_conn, slug, strategy, n=discriminative_n, min_inclusion=min_inclusion
            )
        )
        per_pair[f"{slug}|{strategy}"] = {
            "discriminative_recall": metrics.recall(ranked, disc),
            "core_recall": metrics.recall(ranked, core),
            "novelty_rate": novelty_rate(ranked, labelled),
            "pool_size": pool_size(ports_conn, catalog, name, strategy),
        }
        by_commander.setdefault(slug, {})[strategy] = ranked

    rows = list(per_pair.values())
    n = max(len(rows), 1)
    aggregate = {
        key: sum(r[key] for r in rows) / n
        for key in ("discriminative_recall", "core_recall", "novelty_rate")
    }
    aggregate["divergence"] = sum(divergence(v) for v in by_commander.values()) / max(
        len(by_commander), 1
    )
    return EvalResult(per_pair=per_pair, aggregate=aggregate, params={"top_n": top_n})
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `uv run pytest tests/eval/test_report.py -v`
Expected: 6 passed.

- [ ] **Step 5: Add the `evaluate` subcommand**

```python
def _cmd_evaluate(args: argparse.Namespace) -> int:
    import json as _json

    from .db import open_db as _open_db
    from .eval.report import evaluate
    from .labels.store import open_themes_db
    from .strategy.catalog import load_catalog

    baseline = _json.loads(Path("docs/baseline.json").read_text(encoding="utf-8"))
    pairs = [tuple(p.split("|")) for p in args.pairs.read_text().split()]
    ports, themes = _open_db(args.db, create=False), open_themes_db(args.themes_db, create=False)
    try:
        result = evaluate(
            ports, themes, load_catalog(), pairs,
            top_n=baseline["top_n"], core_floor=baseline["core_floor"],
            discriminative_n=baseline["discriminative_n"], min_inclusion=baseline["min_inclusion"],
        )
    finally:
        ports.close()
        themes.close()

    args.out.write_text(result.to_markdown(), encoding="utf-8")
    gates = baseline["gates"]
    agg = result.aggregate["discriminative_recall"]
    worst = min((v["discriminative_recall"] for v in result.per_pair.values()), default=0.0)
    print(result.to_markdown())
    passed = agg >= gates["aggregate_discriminative_recall"] and worst >= gates["per_commander_minimum"]
    print(f"GATE: {'PASS' if passed else 'FAIL'}  aggregate={agg:.3f} worst={worst:.3f}")
    return 0 if passed else 1
```

Register with `--pairs` (a file of `name|slug|strategy` lines), `--db`, `--themes-db`, `--out` (default `docs/evaluation.md`).

- [ ] **Step 6: Add golden snapshots**

`tests/eval/test_golden.py`:

```python
"""Ranking changes must show up as reviewable diffs, not silent drift.

This is also the **core-change sweep** of spec §7. `core` is the one
surface every strategy shares, so a change there propagates everywhere;
the pairs below deliberately span all four strategies so a core edit
produces a diff across the whole file rather than in one corner.
Strategy-manifest changes touch only their own rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_strategy_graph import recommend

GOLDEN = Path("tests/eval/golden.json")
PAIRS = [
    ("Korvold, Fae-Cursed King", "aristocrats"),
    ("Korvold, Fae-Cursed King", "reanimator"),
    ("Korvold, Fae-Cursed King", "generic"),
    ("Yawgmoth, Thran Physician", "aristocrats"),
    ("Reyhan, Last of the Abzan", "plus1_counters"),
]


@pytest.mark.integration
def test_rankings_match_the_golden_snapshot():
    current = {
        f"{c}|{s}": [x.name for x in recommend(c, s, top_n=30)] for c, s in PAIRS
    }
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip("golden snapshot created; re-run to compare")
    assert current == json.loads(GOLDEN.read_text()), (
        "rankings changed — review the diff, then update tests/eval/golden.json deliberately"
    )
```

- [ ] **Step 7: Run and commit**

Run: `uv run pytest -v && uv run pytest -m integration -v`

```bash
git add -A
git commit -m "feat(eval): metric report, evaluate CLI, golden ranking snapshots"
```

---

### Task 20: Judgment review and the kill-criteria checkpoint

**Files:**
- Create: `docs/evaluation.md`, `docs/judgment.md`
- Modify: `docs/DECISIONS.md`, `README.md`

**Interfaces:**
- Consumes: everything. Produces the decision record that says whether the slice succeeded.

The primary gate is human judgment (spec §6.4). No proxy metric substitutes for it.

- [ ] **Step 1: Generate the comparison report**

For each of the ~20 commanders × their available slice strategies, put the old engine's top-30 beside the new one:

```bash
uv run mtg-strategy-graph evaluate --pairs data/slice_pairs.txt --out docs/evaluation.md
```

For the old side, run the old repo's `scripts/recommend.py --commander "<name>" --top 30` and paste its list into `docs/judgment.md` alongside. One section per pair, two columns.

- [ ] **Step 2: Review every list and mark it**

In `docs/judgment.md` mark each list `good` / `bad` with one sentence of reasoning. This is the gate — do not skip to the metrics.

- [ ] **Step 3: Check all five kill criteria**

Record each in `docs/DECISIONS.md` with the observed value:

1. Does the mined Reanimator signature rank `ChangeZone Graveyard→Battlefield` at the top **across all Reanimator-tagged commanders**? (Not on Korvold alone — his cohort is lands-matter.)
2. Is cross-strategy divergence materially above 0?
3. Did the generalisation guard reject nearly every mined pattern?
4. Did Yawgmoth and Karador move off zero discriminative recall, and did their pool sizes grow past the old engine's 49?
5. Did the gate pass while novelty collapsed toward 0?

Any of these firing means stop and rethink rather than scale to 40 strategies.

- [ ] **Step 4: Write the verdict**

Add a `## 2026-XX-XX — slice verdict` section to `docs/DECISIONS.md` covering: the gate result from `evaluate`, the judgment summary, each kill criterion with its number, and a recommendation for sub-projects B–G. If the slice failed, say so plainly and record which assumption broke — a recorded null result is the most valuable artefact this project produces, as the four predecessor DECLINEs demonstrate.

- [ ] **Step 5: Write the README**

Cover: what the project is, the strategy-partition model in three sentences, how to build `ports.db` and `themes.db`, how to run `recommend`, and — prominently — that EDHREC data is design-time only and never consulted at inference.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: slice evaluation, judgment review, and kill-criteria verdict"
```

---

## Notes for the implementer

**Read the spec first.** `docs/superpowers/specs/2026-08-14-strategy-conditioned-recommendations-design.md` in the `mtg-synergy-graph` repo. Sections §1.1 (interference channels), §3 (core model) and §6 (evaluation) carry reasoning this plan assumes.

**Four things are non-negotiable, because each was learned the expensive way:**

1. **Deny-by-default.** If you find yourself loading a rule to check whether it applies, stop — that is channel (b) creeping back in.
2. **No popularity at inference.** No EDHREC rank in the score and none in the sort key. The predecessor injected it through a `rank_bonus` micro-term and a sort-key tiebreak, and a later diagnostic found those were worth −0.0441 of its headline metric — hidden credit that made mechanical signal impossible to read.
3. **The isolation test may not be weakened.** If it fails, the scorer is wrong.
4. **Pre-registered gates may not be adjusted after measurement.** They are pinned in `docs/baseline.json` by Task 10 before any scoring exists, precisely so they cannot be tuned to the result.

**Where the risk actually is.** Tasks 1–8 are mechanical. Task 15 (mining) is the research bet — if signatures do not separate, the design needs rethinking and it is better to learn that in week one than after 40 strategies. Task 17 is the judgment-heavy one: promoting proposals into rules is where a flood gets in.

**Expected wall-clock:** Tasks 1–4 about a day; 5–10 a day plus the corpus fetch; 11–14 two days; 15–17 the longest and least predictable; 18–20 a day plus the review session.

