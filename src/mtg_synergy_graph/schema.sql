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
    set_code        TEXT,
    -- Scryfall legalities.commander: 1 legal, 0 not_legal/banned. Default 1
    -- preserves behaviour when the Scryfall source DB lacks the column
    -- (tiny test fixtures) — missing data is treated as "no known reason
    -- to exclude".
    legal_commander INTEGER DEFAULT 1,
    -- Forge `CopyFaceFrom:<Name>` directive (back-face of a Prepared card
    -- references an existing card by name). NULL for the vast majority of
    -- cards. Persisted so the importer's second pass can find every carrier
    -- without re-parsing the .txt file.
    -- See docs/brainstorms/2026-05-20-copy-face-from-resolution-requirements.md.
    copy_face_from  TEXT
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
    effect_conditional BOOLEAN DEFAULT FALSE,  -- trigger's execute has runtime Condition* gate
                                                 -- (Selvala's power check, Meren's XP compare)
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

-- ---------------------------------------------------------------------------
-- card_hints: normalised projection of DeckNeeds / DeckHints / DeckHas
-- (from cards.deck_*) and BuffedBy (from card_svars). Populated by the
-- importer; enables rule queries like "commander DeckHas Token Ability
-- intersect candidate DeckNeeds Token".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_hints (
    card_name  TEXT NOT NULL REFERENCES cards(name),
    kind       TEXT NOT NULL,     -- 'needs' | 'hints' | 'has' | 'buffed_by'
    category   TEXT NOT NULL,     -- 'Type' | 'Ability' | 'Color' | 'Keyword' | 'Name'
    value      TEXT NOT NULL,
    PRIMARY KEY (card_name, kind, category, value)
);

CREATE INDEX IF NOT EXISTS idx_card_hints_lookup
    ON card_hints(kind, category, value);

-- ---------------------------------------------------------------------------
-- rule_contributions: persisted per-(commander, candidate, rule) contribution
-- tensor for bench.py. Written by the eval harness, never by the inference
-- path. Enables audit / --rule ablation / --inspect / --collinearity as SQL
-- queries instead of re-scores. See docs/plans/2026-04-22-001-feat-unified-
-- eval-harness-plan.md FR1/FR2.
--
-- config_hash = sha256 over (sorted rule_ids + sorted _RULE_QUALITY_MULTIPLIER
-- + sorted _FLAT_WEIGHT_OVERRIDES). Any scoring-config change produces a new
-- hash; queries filter by the hash so stale rows are never silently read.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rule_contributions (
    commander    TEXT NOT NULL,
    candidate    TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    contribution REAL NOT NULL,
    idf_weight   REAL NOT NULL,
    raw_count    INTEGER NOT NULL,
    config_hash  TEXT NOT NULL,
    computed_at  TEXT NOT NULL,  -- ISO-8601 timestamp
    PRIMARY KEY (commander, candidate, rule_id, config_hash)
);

CREATE INDEX IF NOT EXISTS idx_rule_contributions_rule
    ON rule_contributions(rule_id, config_hash);

CREATE INDEX IF NOT EXISTS idx_rule_contributions_cmdr_hash
    ON rule_contributions(commander, config_hash);

-- ---------------------------------------------------------------------------
-- event_match_map: trigger event → effect event equivalences (plan 003 Unit 3).
-- Rows: (from_event, to_event, match_quality) where match_quality is one
-- of mtg_synergy_graph.port_graph.vocabulary.MATCH_QUALITIES. The table is
-- populated from data/event_match_seed.json by the importer (via
-- port_graph.event_maps.seed_event_match_map_db). Scoring-side callers
-- continue to read the nested Python dict in graph_engine.py, which is
-- itself populated from the same JSON seed at module import — both
-- representations cannot drift because they share the source.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_match_map (
    from_event     TEXT NOT NULL,
    to_event       TEXT NOT NULL,
    match_quality  TEXT NOT NULL,
    PRIMARY KEY (from_event, to_event)
);

CREATE INDEX IF NOT EXISTS idx_event_match_from ON event_match_map(from_event);

-- ---------------------------------------------------------------------------
-- cost_feeds_trigger: cost port event_class → trigger event it feeds.
-- Companion to event_match_map; same seed workflow.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cost_feeds_trigger (
    cost_event     TEXT NOT NULL,
    trigger_event  TEXT NOT NULL,
    PRIMARY KEY (cost_event, trigger_event)
);

CREATE INDEX IF NOT EXISTS idx_cost_feeds_cost ON cost_feeds_trigger(cost_event);

-- ---------------------------------------------------------------------------
-- rules: declarative complement-rule rows consumed by the RuleInterpreter
-- (plan 003 Unit 4 + Unit 5). Each row's gate / commander / candidate
-- predicates are JSON trees built from port_graph.vocabulary.GATE_OPS.
-- Populated from data/rules_seed.json by the importer; empty in Unit 4
-- (migrations land in Units 7 & 8). The rules table coexists with the
-- Python-helper registry: a rule_id lives in ONE of the two, never both.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    rule_id                    TEXT PRIMARY KEY,
    family                     TEXT NOT NULL,
    gate_predicate             TEXT NOT NULL,   -- JSON tree
    commander_port_predicate   TEXT NOT NULL,   -- JSON tree
    candidate_port_predicate   TEXT NOT NULL,   -- JSON tree
    filter_group               TEXT NOT NULL DEFAULT '',
    cmdr_event                 TEXT NOT NULL,
    cand_event                 TEXT NOT NULL,
    weight_hint                REAL NOT NULL DEFAULT 1.0,
    active                     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_rules_family ON rules(family);
CREATE INDEX IF NOT EXISTS idx_rules_active ON rules(active);

-- ---------------------------------------------------------------------------
-- port_nodes: canonical projection over card_ports (plan 003 Unit 2).
-- Maps each (port_type, event_class) pair to a value in
-- mtg_synergy_graph.port_graph.vocabulary.NODE_KINDS; unmapped pairs
-- get node_kind='UNKNOWN'.
--
-- The view DDL is owned by port_graph/projection.py:PORT_NODES_VIEW_SQL
-- and created by mtg_synergy_graph.db.open_db() immediately after the
-- schema.sql executescript — applying the DDL in two places risked
-- silent drift when rule migrations add CASE branches.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- card_embeddings: per-card 128-dim content embedding blob (plan
-- 2026-04-23-003 Unit 2). Populated by scripts/build_embeddings.py from
-- TF-IDF + truncated-SVD over structured port/keyword features. Blob is a
-- numpy float32 array of fixed shape (128,). Read at inference time by
-- embeddings.store.load_card_embeddings; graceful fallback on missing /
-- empty / corrupt rows. Vector rebuild is wholesale (replace-all
-- semantics) whenever the cardsfolder or vectorizer_version changes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_embeddings (
    card_name          TEXT PRIMARY KEY REFERENCES cards(name),
    vector             BLOB NOT NULL,
    vectorizer_version INTEGER NOT NULL,
    built_at           TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- card_embeddings_config: KV store for EmbeddingConfigInputs + derived
-- config_hash (plan 2026-04-23-003 Unit 2). Keys include
-- token_format_version, svd_dims, min_df, vectorizer_version,
-- port_signature_version, config_hash. The config_hash row gates
-- verify_current_or_raise in embeddings.config; individual input rows
-- are written for human diagnostics.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_embeddings_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
