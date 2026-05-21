-- forge_oracle sidecar DB schema.
--
-- This DB is offline infrastructure. It is NEVER opened by the
-- inference path (recommend.py / SynergyEngine.page / universal_scorer
-- / graph_engine). Strict consumers (bench.py audit --vs-forge-oracle,
-- scripts/forge_oracle.py propose-rules) and the soft consumer
-- (scripts/gap_report.py) open it via explicit sqlite3.connect on
-- data/forge_oracle.db.
--
-- Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md.

-- ---------------------------------------------------------------------------
-- forge_precon_ppmi — PPMI co-occurrence table over Forge's bundled precons.
-- Rows canonical with port_signature_a < port_signature_b.
-- Only rows with decks_count >= min_decks_count (threshold 3 by default)
-- are persisted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS forge_precon_ppmi (
    port_signature_a TEXT NOT NULL,
    port_signature_b TEXT NOT NULL,
    ppmi REAL NOT NULL,
    decks_count INTEGER NOT NULL,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (port_signature_a, port_signature_b)
);

CREATE INDEX IF NOT EXISTS idx_forge_precon_ppmi_a ON forge_precon_ppmi(port_signature_a);
CREATE INDEX IF NOT EXISTS idx_forge_precon_ppmi_b ON forge_precon_ppmi(port_signature_b);

-- ---------------------------------------------------------------------------
-- oracle_config — flexible KV store for OracleConfigInputs (Unit 5).
-- Keys include: forge_sha, ppmi_smoothing_k, min_decks_count,
-- vocab_version, java_method_id, config_hash, built_at.
-- The config_hash row gates refuse-to-run in strict consumers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS oracle_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
