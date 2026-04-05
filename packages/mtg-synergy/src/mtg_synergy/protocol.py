"""Card data provider protocol for decoupling inference from card storage.

CardProvider abstracts card data access so the inference library can work with
any backend (SQLite, Postgres/Neon, in-memory dicts, etc.).

SqliteCardProvider is the default implementation backed by the local tags.db.
"""

import json
import sqlite3
from typing import Protocol


class CardProvider(Protocol):
    """Protocol for providing card data to the inference engine.

    Implementations must supply three methods:
    - get_type_lines(): bulk load for init (~34k entries, called once)
    - get_color_legal(): per-request candidate filtering
    - get_commanders(): per-request commander lookup (1-2 for partners)
    """

    def get_type_lines(self) -> dict[str, str]:
        """Return oracle_id -> type_line for all cards.

        Called once at ForgeFeatureContext init and cached. Used for:
        - oid_to_idx index building
        - type_line lookups in feature computation
        - type-based produces in mechanics vectors
        """
        ...

    def get_name_to_oid(self) -> dict[str, str]:
        """Return lowercase_name -> oracle_id for all cards.

        Called once during ForgeFeatureContext init for EDHREC name resolution.
        """
        ...

    def get_color_legal(self, colors: set[str],
                        exclude_oids: set[str] | None = None) -> list[dict]:
        """Return color-legal non-token commander-legal cards.

        Args:
            colors: set of color symbols, e.g. {"R", "G"}. Cards with
                color_identity subset of this set are included.
            exclude_oids: oracle_ids to skip (commander + deck cards).

        Returns list of dicts with keys:
            oracle_id, name, type_line, mana_cost, cmc, color_identity
        where color_identity is a set of color symbols.
        """
        ...

    def get_commanders(self, names: list[str]) -> list[dict] | None:
        """Look up 1-2 commanders by name (case-insensitive).

        Args:
            names: list of 1 or 2 commander names (for partners/backgrounds).

        Returns list of dicts with keys:
            oracle_id, name, type_line, color_identity (set of symbols)
        or None if any commander is not found.
        """
        ...


class SqliteCardProvider:
    """CardProvider backed by the local SQLite cards table in tags.db.

    Drop-in replacement for direct SQL queries. Used by CLI and training.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_type_lines(self) -> dict[str, str]:
        result = {}
        for oid, tl in self._conn.execute(
            "SELECT oracle_id, type_line FROM cards"
        ):
            if tl:
                result[oid] = tl
        return result

    def get_name_to_oid(self) -> dict[str, str]:
        result = {}
        for oid, name in self._conn.execute(
            "SELECT oracle_id, name FROM cards"
        ):
            result[name.lower()] = oid
        return result

    def get_color_legal(self, colors: set[str],
                        exclude_oids: set[str] | None = None) -> list[dict]:
        exclude = exclude_oids or set()
        results = []
        for row in self._conn.execute(
            "SELECT oracle_id, name, type_line, mana_cost, cmc, color_identity "
            "FROM cards "
            "WHERE type_line NOT LIKE '%Token%' AND legal_commander = 1"
        ):
            oid, name, tl, mc, cmc_val, ci_json = row
            if oid in exclude:
                continue
            ci = set(json.loads(ci_json)) if ci_json else set()
            if ci <= colors:
                results.append({
                    "oracle_id": oid,
                    "name": name,
                    "type_line": tl or "",
                    "mana_cost": mc or "",
                    "cmc": cmc_val or 0,
                    "color_identity": ci,
                })
        return results

    def get_commanders(self, names: list[str]) -> list[dict] | None:
        result = []
        for name in names:
            row = self._conn.execute(
                "SELECT oracle_id, name, type_line, color_identity "
                "FROM cards WHERE LOWER(name) = LOWER(?)",
                (name,),
            ).fetchone()
            if row is None:
                return None
            oid, db_name, tl, ci_json = row
            result.append({
                "oracle_id": oid,
                "name": db_name,
                "type_line": tl or "",
                "color_identity": set(json.loads(ci_json or "[]")),
            })
        return result
