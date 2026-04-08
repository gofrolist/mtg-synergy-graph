-- SPEC.md v1.2.2 §4: SQLite schema for the deterministic Forge-DSL graph engine.
-- All tables live in a single file: synergy.db

-- ---------------------------------------------------------------------------
-- §4.1 cards
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cards (
    name            TEXT PRIMARY KEY,
    oracle_id       TEXT,            -- Scryfall canonical oracle_id; NULL only for cards the
                                     -- import-time resolver could not match against tags.db
    mana_cost       TEXT,
    cmc             REAL,
    types           TEXT,            -- space-separated "Creature Human Cleric"
    supertypes      TEXT,            -- "Legendary"
    subtypes        TEXT,            -- "Human Cleric"
    card_types      TEXT,            -- "Creature"
    colors          TEXT,            -- "W,U"
    color_identity  TEXT,            -- "W,U,B"
    power           TEXT,
    toughness       TEXT,
    loyalty         TEXT,
    keywords        TEXT,            -- JSON array
    oracle_text     TEXT,
    is_commander    BOOLEAN DEFAULT FALSE,
    deck_hints      TEXT,            -- JSON
    deck_needs      TEXT,            -- JSON
    deck_has        TEXT,            -- JSON
    edhrec_rank     INTEGER,
    rarity          TEXT,
    set_code        TEXT
);

-- Partial unique index: one Forge card per Scryfall oracle_id, but NULLs are
-- unconstrained (unresolved cards can coexist until a newer tags.db fills them in).
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_oracle_id
    ON cards(oracle_id) WHERE oracle_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- §4.2 card_ports
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_ports (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name          TEXT NOT NULL REFERENCES cards(name),
    port_type          TEXT NOT NULL,     -- trigger|effect|static|replacement|cost|scales_with|keyword
    event_class        TEXT NOT NULL,
    valid_filter       TEXT,
    zone_origin        TEXT,
    zone_destination   TEXT,
    phase              TEXT,
    affected_scope     TEXT,
    effect_zone        TEXT,
    cost_subtype       TEXT,
    cost_target        TEXT,             -- self|other|any (Phase A1)
    trigger_source     TEXT,             -- first_time, etc. (Phase A2)
    mana_restriction   TEXT,             -- DB$ Mana RestrictValid$ (Phase A3)
    amount             TEXT,
    counter_type       TEXT,
    granted_keyword    TEXT,
    granted_ability    TEXT,
    execute_ref        TEXT,
    sub_ability_ref    TEXT,
    is_conditional     BOOLEAN DEFAULT FALSE,
    branch_kind        TEXT DEFAULT 'root',
    branch_parent      TEXT,
    source_svar        TEXT,
    chain_depth        INTEGER DEFAULT 0,
    scaling_expression TEXT,
    is_optional        BOOLEAN DEFAULT FALSE,
    is_combat          BOOLEAN DEFAULT FALSE,
    is_curse           BOOLEAN DEFAULT FALSE,
    replacement_event  TEXT,
    replacement_result TEXT,
    replacement_player TEXT,
    duration           TEXT,
    raw_line           TEXT
);

CREATE INDEX IF NOT EXISTS idx_ports_card          ON card_ports(card_name);
CREATE INDEX IF NOT EXISTS idx_ports_type          ON card_ports(port_type);
CREATE INDEX IF NOT EXISTS idx_ports_event         ON card_ports(event_class);
CREATE INDEX IF NOT EXISTS idx_ports_filter        ON card_ports(valid_filter);
CREATE INDEX IF NOT EXISTS idx_ports_type_event    ON card_ports(port_type, event_class);
CREATE INDEX IF NOT EXISTS idx_ports_conditional   ON card_ports(is_conditional, branch_kind);
CREATE INDEX IF NOT EXISTS idx_ports_scaling_expr  ON card_ports(scaling_expression);

-- ---------------------------------------------------------------------------
-- §4.3 synergy_edges
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS synergy_edges (
    commander              TEXT NOT NULL REFERENCES cards(name),
    card                   TEXT NOT NULL REFERENCES cards(name),
    total_score            REAL NOT NULL,
    port_match_score       REAL DEFAULT 0,
    reverse_match_score    REAL DEFAULT 0,
    internal_synergy_score REAL DEFAULT 0,
    chain_score            REAL DEFAULT 0,
    lord_score             REAL DEFAULT 0,
    cost_synergy_score     REAL DEFAULT 0,
    scaling_score          REAL DEFAULT 0,
    replacement_score      REAL DEFAULT 0,
    amplifier_score        REAL DEFAULT 0,
    strategic_score        REAL DEFAULT 0,
    staple_score           REAL DEFAULT 0,
    edhrec_score           REAL DEFAULT 0,
    graph_neighbor_overlap REAL DEFAULT 0,
    graph_pagerank         REAL DEFAULT 0,
    card_hub_score         REAL DEFAULT 0,
    cmdr_2hop_ratio        REAL DEFAULT 0,
    explanation            TEXT,
    PRIMARY KEY (commander, card)
);

CREATE INDEX IF NOT EXISTS idx_synergy_commander
    ON synergy_edges(commander, total_score DESC);

-- ---------------------------------------------------------------------------
-- §4.4 card_svars
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_svars (
    card_name  TEXT NOT NULL REFERENCES cards(name),
    svar_name  TEXT NOT NULL,
    svar_value TEXT NOT NULL,
    PRIMARY KEY (card_name, svar_name)
);

-- ---------------------------------------------------------------------------
-- §4.6 port_attributes (attribute normalization)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS port_attributes (
    port_id    INTEGER NOT NULL REFERENCES card_ports(id) ON DELETE CASCADE,
    attr_kind  TEXT NOT NULL,          -- type|subtype|color|keyword|supertype|cmc_cmp|power_cmp|controller|zone|counter_type|unknown
    attr_value TEXT NOT NULL,
    is_negated BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (port_id, attr_kind, attr_value, is_negated)
);

CREATE INDEX IF NOT EXISTS idx_port_attr_lookup  ON port_attributes(attr_kind, attr_value, is_negated);
CREATE INDEX IF NOT EXISTS idx_port_attr_by_port ON port_attributes(port_id);

-- ---------------------------------------------------------------------------
-- §6.8 / Phase 4.4 — precomputed graph metrics cache
-- ---------------------------------------------------------------------------
-- Per-card causal-graph features. Built once after import via
-- mtg_synergy_graph.graph_cache.build_graph_cache(). The query-time engine
-- joins this table when SynergyEngine(..., graph_metrics=True).
CREATE TABLE IF NOT EXISTS graph_cache (
    card_name      TEXT PRIMARY KEY REFERENCES cards(name),
    hub_score      REAL NOT NULL DEFAULT 0,  -- normalised degree centrality, 0..1
    pagerank       REAL NOT NULL DEFAULT 0,  -- non-personalised PageRank, 0..1
    degree         INTEGER NOT NULL DEFAULT 0,
    built_at       TEXT NOT NULL             -- ISO timestamp of last build
);

CREATE INDEX IF NOT EXISTS idx_graph_cache_pagerank
    ON graph_cache(pagerank DESC);

-- One row per undirected edge, capped to a configurable per-card top-K so
-- the file stays small. Used by per-pair metrics (Jaccard / 2-hop) at
-- query time when graph_metrics is enabled.
CREATE TABLE IF NOT EXISTS causal_neighbours (
    card_name      TEXT NOT NULL REFERENCES cards(name),
    neighbour_name TEXT NOT NULL,
    PRIMARY KEY (card_name, neighbour_name)
);

CREATE INDEX IF NOT EXISTS idx_causal_neighbours_by_card
    ON causal_neighbours(card_name);
