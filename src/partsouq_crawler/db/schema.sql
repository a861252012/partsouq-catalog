CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    seed_urls_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    blocked_reason TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    pages_discovered INTEGER NOT NULL DEFAULT 0,
    pages_done INTEGER NOT NULL DEFAULT 0,
    pages_failed INTEGER NOT NULL DEFAULT 0,
    pages_challenged INTEGER NOT NULL DEFAULT 0,
    records_extracted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS crawl_queue (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    requested_url TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    parent_url TEXT,
    depth INTEGER NOT NULL,
    page_type_hint TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_expires_at TEXT,
    next_attempt_at TEXT,
    last_error TEXT,
    response_id INTEGER,
    discovered_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, url_hash)
);

CREATE INDEX IF NOT EXISTS idx_crawl_queue_schedule
ON crawl_queue(run_id, status, priority DESC, next_attempt_at);

CREATE TABLE IF NOT EXISTS discovery_edges (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    source_response_id INTEGER,
    parent_url TEXT,
    discovered_url TEXT NOT NULL,
    discovery_method TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    UNIQUE(run_id, parent_url, discovered_url, discovery_method)
);

CREATE TABLE IF NOT EXISTS response_bodies (
    sha256 TEXT PRIMARY KEY,
    compression TEXT NOT NULL,
    body_blob BLOB NOT NULL,
    original_bytes INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_responses (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    queue_id INTEGER REFERENCES crawl_queue(id) ON DELETE SET NULL,
    requested_url TEXT NOT NULL,
    final_url TEXT NOT NULL,
    redirect_chain_json TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    response_headers_json TEXT NOT NULL,
    content_type TEXT,
    charset TEXT,
    body_sha256 TEXT NOT NULL REFERENCES response_bodies(sha256),
    response_bytes INTEGER NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    is_cloudflare_challenge INTEGER NOT NULL DEFAULT 0,
    challenge_reason TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_http_responses_run_url
ON http_responses(run_id, requested_url);

CREATE TABLE IF NOT EXISTS archive_captures (
    id INTEGER PRIMARY KEY,
    response_id INTEGER NOT NULL UNIQUE REFERENCES http_responses(id) ON DELETE CASCADE,
    archive_source TEXT NOT NULL,
    collection_name TEXT,
    captured_at TEXT NOT NULL,
    warc_filename TEXT,
    warc_offset INTEGER,
    warc_length INTEGER,
    archive_digest TEXT,
    truncation_reason TEXT,
    metadata_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_captures_source_time
ON archive_captures(archive_source, captured_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_captures_warc_location
ON archive_captures(
    archive_source, collection_name, warc_filename, warc_offset, warc_length
);

CREATE TABLE IF NOT EXISTS record_sources (
    id INTEGER PRIMARY KEY,
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    response_id INTEGER NOT NULL REFERENCES http_responses(id) ON DELETE CASCADE,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_url TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    UNIQUE(record_type, record_id, response_id, parser_name, parser_version)
);

CREATE TABLE IF NOT EXISTS vehicle_configurations (
    id INTEGER PRIMARY KEY,
    catalog_brand TEXT,
    brand_raw TEXT,
    brand_normalized TEXT,
    name_raw TEXT,
    model_raw TEXT,
    description_raw TEXT,
    options_raw TEXT,
    prod_period_raw TEXT,
    production_from TEXT,
    production_to TEXT,
    production_precision TEXT,
    catalog_code TEXT,
    vehicle_external_id TEXT,
    metadata_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(catalog_brand, vehicle_external_id, model_raw, prod_period_raw, source_url)
);

CREATE TABLE IF NOT EXISTS taxonomy_nodes (
    id INTEGER PRIMARY KEY,
    vehicle_configuration_id INTEGER NOT NULL REFERENCES vehicle_configurations(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES taxonomy_nodes(id) ON DELETE CASCADE,
    depth INTEGER NOT NULL,
    code_raw TEXT,
    name_raw TEXT NOT NULL,
    path_raw TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(vehicle_configuration_id, path_raw)
);

CREATE TABLE IF NOT EXISTS diagrams (
    id INTEGER PRIMARY KEY,
    vehicle_configuration_id INTEGER NOT NULL REFERENCES vehicle_configurations(id) ON DELETE CASCADE,
    taxonomy_node_id INTEGER REFERENCES taxonomy_nodes(id) ON DELETE SET NULL,
    diagram_code_raw TEXT,
    diagram_name_raw TEXT,
    diagram_range_raw TEXT,
    diagram_from TEXT,
    diagram_to TEXT,
    metadata_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(vehicle_configuration_id, diagram_code_raw, diagram_name_raw, diagram_range_raw, source_url)
);

CREATE TABLE IF NOT EXISTS part_numbers (
    id INTEGER PRIMARY KEY,
    part_brand_raw TEXT,
    number_raw TEXT NOT NULL,
    number_normalized TEXT NOT NULL,
    name_en_raw TEXT,
    is_assembly_inferred INTEGER NOT NULL DEFAULT 0,
    assembly_inference_reason TEXT,
    source_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(part_brand_raw, number_raw, source_url)
);

CREATE INDEX IF NOT EXISTS idx_part_numbers_search
ON part_numbers(part_brand_raw, number_normalized);

CREATE TABLE IF NOT EXISTS part_occurrences (
    id INTEGER PRIMARY KEY,
    part_number_id INTEGER NOT NULL REFERENCES part_numbers(id) ON DELETE CASCADE,
    diagram_id INTEGER NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
    vehicle_configuration_id INTEGER NOT NULL REFERENCES vehicle_configurations(id) ON DELETE CASCADE,
    callout_raw TEXT,
    quantity_raw TEXT,
    part_range_raw TEXT,
    part_from TEXT,
    part_to TEXT,
    part_condition_raw TEXT,
    note_raw TEXT,
    row_metadata_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(part_number_id, diagram_id, callout_raw, quantity_raw, part_range_raw, part_condition_raw, note_raw, source_url)
);

CREATE TABLE IF NOT EXISTS fitments (
    id INTEGER PRIMARY KEY,
    part_occurrence_id INTEGER NOT NULL REFERENCES part_occurrences(id) ON DELETE CASCADE,
    part_number_id INTEGER NOT NULL REFERENCES part_numbers(id) ON DELETE CASCADE,
    vehicle_configuration_id INTEGER NOT NULL REFERENCES vehicle_configurations(id) ON DELETE CASCADE,
    diagram_id INTEGER NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
    is_verified INTEGER NOT NULL,
    derivation TEXT NOT NULL,
    confidence REAL NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    source_url TEXT NOT NULL,
    UNIQUE(part_occurrence_id, derivation)
);

CREATE TABLE IF NOT EXISTS compatibility_hints (
    id INTEGER PRIMARY KEY,
    part_number_id INTEGER NOT NULL REFERENCES part_numbers(id) ON DELETE CASCADE,
    brand_text TEXT,
    model_text TEXT,
    compatibility_text TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(part_number_id, compatibility_text, source_url)
);

CREATE TABLE IF NOT EXISTS part_relations (
    id INTEGER PRIMARY KEY,
    from_part_number_id INTEGER NOT NULL REFERENCES part_numbers(id) ON DELETE CASCADE,
    to_part_number_raw TEXT NOT NULL,
    to_part_number_normalized TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    relation_text TEXT,
    confidence REAL NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(from_part_number_id, to_part_number_raw, relation_type, source_url)
);

CREATE TABLE IF NOT EXISTS parse_failures (
    id INTEGER PRIMARY KEY,
    response_id INTEGER NOT NULL REFERENCES http_responses(id) ON DELETE CASCADE,
    parser_name TEXT NOT NULL,
    page_type TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    selector_context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(response_id, parser_name, error_type, error_message)
);

CREATE TABLE IF NOT EXISTS robots_snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    response_id INTEGER NOT NULL REFERENCES http_responses(id) ON DELETE CASCADE,
    user_agent TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(run_id, response_id, user_agent)
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
